"""Отправка в MAX от имени агента: только после согласования человеком (0112)."""

import inspect

import pytest
from fastapi import HTTPException

from app.agents.autonomy import outbox, runner
from app.tasks.models import Task, TaskStatus


def _card(db, admin):
    task = outbox.propose_message(
        db, chat_id=27580426, text="Тест агента", recipient="Арсений Щекин", recipient_ids=[admin.id]
    )
    db.commit()
    return task


def test_proposal_sends_nothing(db, make_user):
    admin = make_user(admin=True)
    sent = []
    task = _card(db, admin)
    assert task.status == TaskStatus.IN_REVIEW
    assert sent == []


def test_approve_sends_once_and_closes(db, make_user):
    admin = make_user(admin=True)
    task = _card(db, admin)
    sent = []

    def sender(chat_id, text):
        sent.append((chat_id, text))
        return {"chatId": chat_id}

    outbox.approve_and_send(db, task.id, admin, sender)
    assert sent == [(27580426, "Тест агента")]
    assert db.get(Task, task.id).status == TaskStatus.DONE
    with pytest.raises(HTTPException) as exc:
        outbox.approve_and_send(db, task.id, admin, sender)
    assert exc.value.status_code == 409
    assert len(sent) == 1


def test_failed_send_keeps_card_open(db, make_user):
    admin = make_user(admin=True)
    task = _card(db, admin)

    def broken(chat_id, text):
        raise HTTPException(status_code=502, detail="MAX недоступен")

    with pytest.raises(HTTPException):
        outbox.approve_and_send(db, task.id, admin, broken)
    assert db.get(Task, task.id).status == TaskStatus.IN_REVIEW


def test_reject_does_not_send(db, make_user):
    admin = make_user(admin=True)
    task = _card(db, admin)
    outbox.reject(db, task.id, admin)
    assert db.get(Task, task.id).status == TaskStatus.DONE
    with pytest.raises(HTTPException):
        outbox.approve_and_send(db, task.id, admin, lambda *_: pytest.fail("не должно отправляться"))


def test_ordinary_proposal_cannot_be_used_to_send(db, make_user):
    admin = make_user(admin=True)
    from app.agents.autonomy.playbook import Finding
    from app.agents.autonomy.publish import publish_level_b

    res = publish_level_b(db, Finding(scenario="x", key="k", section="clients", level="B", title="t", text="x", recipients=[admin.id]))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        outbox.approve_and_send(db, res["task_id"], admin, lambda *_: pytest.fail("не должно отправляться"))
    assert exc.value.status_code == 404


def test_autonomous_run_has_no_path_to_max():
    assert "outbox" not in inspect.getsource(runner)
    assert "app.max" not in inspect.getsource(runner)
