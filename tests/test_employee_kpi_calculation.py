"""Формула KPI сотрудника (задача 0042, заменяет случайную заглушку 0041):
доля задач (`app.tasks`) с прошедшим дедлайном, выполненных в срок (вес 1)
или с опозданием (вес 0.5); невыполненные — 0. `None`, если оценённых задач
за период нет — см. `backlog/DONE/0042-employee-kpi-calculation.md`.
"""
from datetime import datetime, timedelta, timezone

from app.accounting import service as accounting_service
from app.accounting.models import EmployeeKpi
from app.tasks import service as task_service
from app.tasks import timelog
from app.tasks.models import TaskStatus, TaskWorkDuration


def _now():
    return datetime.now(timezone.utc)


def _close_task(db, task, employee):
    """Проводит задачу без проверяющих через полный цикл — авто-DONE на
    выходе из in_review (см. app.tasks.service._finalize_status)."""
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, employee)
    task_service.set_status(db, task, TaskStatus.IN_REVIEW, employee)
    db.commit()


def _backdate_completion(db, task, completed_at):
    """Сдвинуть весь журнал переходов так, чтобы момент DONE пришёлся ровно на
    `completed_at`, сохранив порядок событий — тот же приём, что
    test_task_time_tracking.py::_backdate, иначе `_recompute` пересортирует
    события по created_at и последним окажется не DONE."""
    from app.tasks.models import TaskStageEvent

    row = db.get(TaskWorkDuration, task.id)
    assert row is not None and row.is_complete

    events = (
        db.query(TaskStageEvent)
        .filter(TaskStageEvent.task_id == task.id)
        .order_by(TaskStageEvent.created_at, TaskStageEvent.id)
        .all()
    )
    for offset, event in enumerate(reversed(events)):
        event.created_at = completed_at - timedelta(seconds=offset)
    db.flush()
    timelog._recompute(db, task)
    db.commit()


def _period_now():
    return accounting_service._month_range(_now().date())


def test_kpi_weighs_on_time_late_and_overdue_tasks(db, make_user):
    employee = make_user()
    period_start, period_end = _period_now()
    deadline = _now() - timedelta(days=2)

    on_time_task = task_service.create_task(
        db, title="Сдать в срок", deadline=deadline, assignee_ids=[employee.id]
    )
    late_task = task_service.create_task(
        db, title="Сдать с опозданием", deadline=deadline, assignee_ids=[employee.id]
    )
    overdue_task = task_service.create_task(
        db, title="Не сделать", deadline=deadline, assignee_ids=[employee.id]
    )
    db.commit()

    _close_task(db, on_time_task, employee)
    _backdate_completion(db, on_time_task, deadline - timedelta(hours=1))

    _close_task(db, late_task, employee)
    _backdate_completion(db, late_task, deadline + timedelta(hours=1))

    # overdue_task остаётся в ready — не закрыта, дедлайн прошёл.

    row = accounting_service._compute_kpi_for_period(db, employee.id, period_start, period_end)
    db.commit()

    assert row.tasks_on_time == 1
    assert row.tasks_late == 1
    assert row.tasks_overdue == 1
    assert row.tasks_total == 3
    # round(100 * (1 + 0.5) / 3) = round(50.0) = 50
    assert row.kpi == 50


def test_kpi_ignores_tasks_without_passed_deadline(db, make_user):
    employee = make_user()
    period_start, period_end = _period_now()

    future_task = task_service.create_task(
        db, title="Дедлайн ещё не наступил", deadline=_now() + timedelta(days=5), assignee_ids=[employee.id]
    )
    no_deadline_task = task_service.create_task(
        db, title="Без дедлайна", assignee_ids=[employee.id]
    )
    db.commit()
    assert future_task.id and no_deadline_task.id

    row = accounting_service._compute_kpi_for_period(db, employee.id, period_start, period_end)
    db.commit()

    assert row.tasks_total == 0
    assert row.kpi is None


def test_kpi_current_period_is_overwritten_not_duplicated(db, make_user):
    """Текущий месяц пересчитывается на месте (upsert), не копится строками —
    только прошлые периоды застывают («история не переписывается»)."""
    employee = make_user()
    period_start, period_end = _period_now()
    deadline = _now() - timedelta(hours=5)

    task = task_service.create_task(db, title="Задача", deadline=deadline, assignee_ids=[employee.id])
    db.commit()
    accounting_service._compute_kpi_for_period(db, employee.id, period_start, period_end)
    db.commit()

    _close_task(db, task, employee)
    _backdate_completion(db, task, deadline - timedelta(hours=1))
    accounting_service._compute_kpi_for_period(db, employee.id, period_start, period_end)
    db.commit()

    rows = (
        db.query(EmployeeKpi)
        .filter(EmployeeKpi.employee_id == employee.id, EmployeeKpi.period_start == period_start)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].kpi == 100
