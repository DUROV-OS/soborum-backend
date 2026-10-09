"""Генерация уведомлений (0080-c): хуки по разделам и плановая проверка
сроков.

Сценарий из backlog/PROCESS/0080-c-notification-triggers.md:
- нет дублей при повторном прогоне плановой проверки сроков задач;
- смена стадии клиента уведомляет менеджера (и не уведомляет, если
  менеджера нет);
- MAX-сообщение уведомляет менеджера чата, но не отправителя.
"""

from datetime import datetime, timedelta, timezone

from app.clients import service as client_service
from app.clients.models import ClientChatLink
from app.clients.schemas import ClientChatLinkCreate, ClientCreate, ClientManagerUpdate
from app.common.module_access import Module
from app.jobs.deadlines import check_task_deadlines
from app.jobs.models import DeadlineThreshold, TaskDeadlineAlert
from app.notifications.models import Notification, NotificationKind
from app.tasks.models import Task, TaskPriority, TaskStatus


def _client(db, *, manager_id=None):
    client = client_service.create_client(
        db, ClientCreate(full_name="Иван Тест", phone="+70000000000", email="ivan@example.com")
    )
    if manager_id is not None:
        client_service.update_manager(db, client, ClientManagerUpdate(manager_id=manager_id))
    db.commit()
    return client


def _task(db, *, responsible_id=None, deadline):
    task = Task(
        title="Проверить смету",
        status=TaskStatus.IN_PROGRESS,
        priority=TaskPriority.MEDIUM,
        deadline=deadline,
        responsible_id=responsible_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# --- Плановая проверка сроков: без дублей -----------------------------------


def test_due_soon_notifies_responsible_once(db, make_user):
    a = make_user(Module.TASKS)
    b = make_user(Module.TASKS)
    task = _task(db, responsible_id=a.id, deadline=datetime.now(timezone.utc) + timedelta(hours=1))

    result = check_task_deadlines(db)
    assert result == {"checked": 1, "due_soon": 1, "overdue": 0}

    notifications_a = db.query(Notification).filter(Notification.user_id == a.id).all()
    assert len(notifications_a) == 1
    assert notifications_a[0].kind == NotificationKind.TASK_DUE
    assert db.query(Notification).filter(Notification.user_id == b.id).count() == 0

    # Повторный прогон не плодит вторую запись по тому же порогу.
    result_again = check_task_deadlines(db)
    assert result_again == {"checked": 1, "due_soon": 0, "overdue": 0}
    assert db.query(Notification).filter(Notification.user_id == a.id).count() == 1
    assert (
        db.query(TaskDeadlineAlert)
        .filter(TaskDeadlineAlert.task_id == task.id, TaskDeadlineAlert.threshold == DeadlineThreshold.DUE_SOON)
        .count()
        == 1
    )


def test_overdue_notifies_exactly_once_after_due_soon(db, make_user):
    a = make_user(Module.TASKS)
    task = _task(db, responsible_id=a.id, deadline=datetime.now(timezone.utc) + timedelta(minutes=1))

    check_task_deadlines(db)  # скоро дедлайн
    task.deadline = datetime.now(timezone.utc) - timedelta(hours=1)  # срок прошёл
    db.commit()
    result = check_task_deadlines(db)

    assert result["overdue"] == 1
    overdue_notifications = (
        db.query(Notification).filter(Notification.user_id == a.id, Notification.kind == NotificationKind.TASK_OVERDUE).all()
    )
    assert len(overdue_notifications) == 1

    # Повторный прогон на уже просроченной задаче не создаёт вторую запись.
    check_task_deadlines(db)
    assert (
        db.query(Notification).filter(Notification.user_id == a.id, Notification.kind == NotificationKind.TASK_OVERDUE).count()
        == 1
    )


def test_deadline_check_skips_task_without_responsible(db):
    _task(db, responsible_id=None, deadline=datetime.now(timezone.utc) + timedelta(minutes=1))

    result = check_task_deadlines(db)

    assert result == {"checked": 1, "due_soon": 0, "overdue": 0}
    assert db.query(Notification).count() == 0
    # Отметка по-прежнему ставится — следующий прогон эту задачу не пересчитывает.
    assert db.query(TaskDeadlineAlert).count() == 1


def test_deadline_check_ignores_done_tasks(db, make_user):
    a = make_user(Module.TASKS)
    task = _task(db, responsible_id=a.id, deadline=datetime.now(timezone.utc) - timedelta(hours=1))
    task.status = TaskStatus.DONE
    db.commit()

    result = check_task_deadlines(db)

    assert result == {"checked": 0, "due_soon": 0, "overdue": 0}
    assert db.query(Notification).count() == 0


# --- Смена стадии клиента ----------------------------------------------------


def test_client_stage_change_notifies_manager(db, make_user):
    manager = make_user(Module.CLIENTS)
    client = _client(db, manager_id=manager.id)

    client_service.transition_stage(db, client)  # LEAD -> DISCUSSION

    notifications = db.query(Notification).filter(Notification.user_id == manager.id).all()
    assert len(notifications) == 1
    assert notifications[0].kind == NotificationKind.CLIENT_UPDATE
    assert notifications[0].object_id == client.id


def test_client_stage_change_without_manager_notifies_nobody(db):
    client = _client(db)
    assert client.manager_id is None

    client_service.transition_stage(db, client)  # не должно упасть и не должно уведомлять

    assert db.query(Notification).count() == 0


# --- MAX: новое сообщение в привязанном чате --------------------------------


def test_max_message_notifies_chat_manager(db, make_user):
    manager = make_user(Module.CLIENTS)
    client = _client(db, manager_id=manager.id)
    link = client_service.create_chat_link(
        db, client, ClientChatLinkCreate(max_chat_id=555, label="С клиентом")
    )
    db.commit()

    client_service.notify_chat_message(db, chat_id=link.max_chat_id, sender_id="999", viewer_id="1")

    notifications = db.query(Notification).filter(Notification.user_id == manager.id).all()
    assert len(notifications) == 1
    assert notifications[0].kind == NotificationKind.MAX_MESSAGE
    assert notifications[0].object_id == client.id


def test_max_message_from_our_own_account_does_not_notify(db, make_user):
    manager = make_user(Module.CLIENTS)
    client = _client(db, manager_id=manager.id)
    link = client_service.create_chat_link(
        db, client, ClientChatLinkCreate(max_chat_id=556, label="С клиентом")
    )
    db.commit()

    # sender_id == viewer_id — это наш собственный исходящий ответ.
    client_service.notify_chat_message(db, chat_id=link.max_chat_id, sender_id="1", viewer_id="1")

    assert db.query(Notification).count() == 0


def test_max_message_on_unlinked_chat_is_a_noop(db):
    client_service.notify_chat_message(db, chat_id=4242, sender_id="999", viewer_id="1")

    assert db.query(Notification).count() == 0
