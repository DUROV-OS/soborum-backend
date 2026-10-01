"""Задачи менеджера по клиенту (0079-d):

- задача заводится со сроком и попадает в общий раздел «Задачи»;
- открытая блокирующая задача не пускает клиента на следующую стадию,
  неблокирующая — пускает;
- срок переносится только с причиной, причина уходит в журнал задачи;
- закрытие требует описания решения и умеет сразу завести вытекающую задачу;
- закрытая задача больше не блокирует и не переносится.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.clients import service as client_service
from app.clients.schemas import (
    ClientCreate,
    ClientTaskClose,
    ClientTaskCreate,
    ClientTaskDeadlineUpdate,
)
from app.common.module_access import Module
from app.tasks.models import Task, TaskLinkType, TaskReportKind, TaskStatus
from app.users.models import User, UserRole


def _tomorrow():
    return datetime.now(timezone.utc) + timedelta(days=1)


def _actor(db):
    user = User(
        email=f"manager{db.query(User).count()}@example.com",
        full_name="Менеджер",
        hashed_password="not-a-login-password",
        is_active=True,
        role=UserRole.WORKER,
    )
    db.add(user)
    db.flush()
    return user


def _client(db):
    return client_service.create_client(
        db, ClientCreate(full_name="Иван Тест", phone="+70000000000", email="ivan@example.com")
    )


def test_task_is_created_with_deadline_and_default_assignee(db):
    client, actor = _client(db), _actor(db)
    task = client_service.create_followup_task(
        db, client, ClientTaskCreate(title="  Выслать каталог  ", deadline=_tomorrow()), actor
    )
    db.commit()

    assert task.title == "Выслать каталог"
    assert task.deadline is not None
    assert [u.id for u in task.assignees] == [actor.id]
    assert task.link_type == TaskLinkType.CLIENT_FOLLOWUP
    assert task.link_meta == {"stage": "lead", "blocking": True}
    # задача обычная — она же лежит в общем списке задач
    assert db.query(Task).filter(Task.id == task.id).one().title == "Выслать каталог"


def test_blocking_task_holds_the_client_on_stage(db):
    client, actor = _client(db), _actor(db)
    client_service.create_followup_task(
        db, client, ClientTaskCreate(title="Связаться", deadline=_tomorrow()), actor
    )
    db.commit()

    with pytest.raises(HTTPException) as err:
        client_service.transition_stage(db, client)
    assert "Связаться" in err.value.detail


def test_non_blocking_task_does_not_hold_the_client(db):
    client, actor = _client(db), _actor(db)
    client_service.create_followup_task(
        db,
        client,
        ClientTaskCreate(title="Напомнить о себе", deadline=_tomorrow(), blocking=False),
        actor,
    )
    db.commit()

    client_service.transition_stage(db, client)
    assert client.stage.value == "discussion"


def test_deadline_shift_needs_reason_and_lands_in_the_log(db):
    client, actor = _client(db), _actor(db)
    task = client_service.create_followup_task(
        db, client, ClientTaskCreate(title="Уточнить по ипотеке", deadline=_tomorrow()), actor
    )
    db.commit()

    with pytest.raises(HTTPException):
        client_service.shift_followup_deadline(
            db, task, ClientTaskDeadlineUpdate(deadline=_tomorrow(), reason="   "), actor
        )

    new_deadline = _tomorrow() + timedelta(days=6)
    client_service.shift_followup_deadline(
        db, task, ClientTaskDeadlineUpdate(deadline=new_deadline, reason="банк просит справку"), actor
    )
    db.commit()

    assert task.deadline.date() == new_deadline.date()
    shift = [r for r in task.reports if r.kind == TaskReportKind.DEADLINE_SHIFT]
    assert len(shift) == 1
    assert "банк просит справку" in shift[0].comment


def test_close_records_resolution_and_can_spawn_the_next_task(db):
    client, actor = _client(db), _actor(db)
    task = client_service.create_followup_task(
        db, client, ClientTaskCreate(title="Связаться", deadline=_tomorrow()), actor
    )
    db.commit()

    with pytest.raises(HTTPException):
        client_service.close_followup_task(db, client, task, ClientTaskClose(resolution="  "), actor)

    client_service.close_followup_task(
        db,
        client,
        task,
        ClientTaskClose(
            resolution="дозвонился, просит каталог",
            next_task=ClientTaskCreate(title="Выслать каталог", deadline=_tomorrow()),
        ),
        actor,
    )
    db.commit()

    assert task.status == TaskStatus.DONE
    assert any("просит каталог" in r.comment for r in task.reports)

    open_tasks = client_service.open_followup_tasks(db, client.id)
    assert [t.title for t in open_tasks] == ["Выслать каталог"]

    # закрытая задача больше не переносится
    with pytest.raises(HTTPException):
        client_service.shift_followup_deadline(
            db, task, ClientTaskDeadlineUpdate(deadline=_tomorrow(), reason="ещё разок"), actor
        )


def test_task_endpoints_require_edit_access(api, make_user, db):
    client = _client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    created = worker.post(
        f"/api/clients/{client.id}/tasks",
        json={"title": "Выслать каталог", "deadline": _tomorrow().isoformat()},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    assert created.json()["blocking"] is True
    assert created.json()["stage"] == "lead"

    blocked = worker.post(f"/api/clients/{client.id}/transition")
    assert blocked.status_code == 400
    assert "Выслать каталог" in blocked.json()["detail"]

    shifted = worker.post(
        f"/api/clients/{client.id}/tasks/{task_id}/deadline",
        json={"deadline": (_tomorrow() + timedelta(days=3)).isoformat(), "reason": "клиент в отпуске"},
    )
    assert shifted.status_code == 200, shifted.text
    assert any("клиент в отпуске" in r["comment"] for r in shifted.json()["reports"])

    closed = worker.post(
        f"/api/clients/{client.id}/tasks/{task_id}/close",
        json={"resolution": "каталог отправлен"},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "done"

    # после закрытия переход проходит, и задача видна в карточке клиента
    assert worker.post(f"/api/clients/{client.id}/transition").status_code == 200
    card = worker.get(f"/api/clients/{client.id}")
    assert [t["title"] for t in card.json()["tasks"]] == ["Выслать каталог"]
