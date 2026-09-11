"""Раздел «Бухгалтерия» в детерминированной сводке «Пульса»
(app.dashboard.overview) — черновики проводок как реальный, не выдуманный
сигнал внимания (используется разделом «Работа» вместо замоканного совета).
"""

from app.accounting.schemas import MoneyMovementCreate
from app.accounting.service import create_money_movement
from app.common.module_access import Module
from app.dashboard.overview import generate_section_signal, generate_today


def test_accounting_widget_and_action_reflect_real_drafts(db, make_user):
    user = make_user(Module.ACCOUNTING, admin=True)
    employee = make_user()

    out = generate_today(db, user)
    assert not any(a.section == "accounting" for a in out.actions)

    create_money_movement(
        db,
        MoneyMovementCreate(subkind="salary_payout", amount=1000, employee_id=employee.id),
        initiator_id=user.id,
    )

    out = generate_today(db, user)
    widget = next(w for w in out.widgets if w.section == "accounting" and w.title == "Черновиков без согласования")
    assert widget.value == "1"
    assert widget.tone == "warning"

    action = next(a for a in out.actions if a.section == "accounting")
    assert action.count == 1
    assert action.href == "/accounting"


def test_accounting_widget_absent_without_module_access(db, make_user):
    user = make_user(admin=False)
    out = generate_today(db, user)
    assert not any(w.section == "accounting" for w in out.widgets)


def test_section_signal_reflects_real_data_and_is_cached(db, make_user):
    user = make_user(Module.ACCOUNTING, admin=True)
    employee_a = make_user()
    employee_b = make_user()

    empty = generate_section_signal(db, user, "accounting")
    assert empty.action is None

    create_money_movement(
        db,
        MoneyMovementCreate(subkind="salary_payout", amount=1000, employee_id=employee_a.id),
        initiator_id=user.id,
    )

    # Кеш ещё не тронут этим вызовом с force — свежий пересчёт видит черновик.
    fresh = generate_section_signal(db, user, "accounting", force=True)
    assert fresh.action is not None
    assert fresh.action.count == 1

    # Без force — отдаёт закешированный ответ (тот, что был до второй проводки).
    create_money_movement(
        db,
        MoneyMovementCreate(subkind="salary_payout", amount=2000, employee_id=employee_b.id),
        initiator_id=user.id,
    )
    cached = generate_section_signal(db, user, "accounting")
    assert cached.action.count == 1

    # force=True снова пересчитывает и обновляет кеш.
    updated = generate_section_signal(db, user, "accounting", force=True)
    assert updated.action.count == 2


def test_section_signal_hides_data_without_access(db, make_user):
    outsider = make_user(admin=False)
    out = generate_section_signal(db, outsider, "accounting")
    assert out.action is None


def test_section_signal_unknown_section_is_empty_not_error(db, make_user):
    user = make_user(admin=True)
    out = generate_section_signal(db, user, "meetings")
    assert out.section == "meetings"
    assert out.action is None


def test_section_signal_users_requires_admin_role(db, make_user):
    worker = make_user(admin=False)
    admin = make_user(admin=True)
    assert generate_section_signal(db, worker, "users").action is None
    # у свежесозданного admin нет workers без доступа выше нуля — просто не падает
    generate_section_signal(db, admin, "users")
