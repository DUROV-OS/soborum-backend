"""Задача 0080-e: подписки на браузерный push — сохранение/отписка через API
и удаление невалидной подписки после неудачной отправки при создании
уведомления. Сетевых вызовов тут нет — `pywebpush.webpush` мокается."""

from unittest.mock import patch

import pytest
from pywebpush import WebPushException

from app.common.module_access import Module
from app.core.config import settings
from app.notifications.models import NotificationKind, PushSubscription
from app.notifications.service import notify


@pytest.fixture
def vapid_configured():
    """Push отправляется только когда настроены VAPID-ключи
    (`settings.push_configured`) — в обычном тестовом окружении (conftest)
    они пустые, поэтому `notify()` молча не шлёт push. Здесь включаем их
    на время теста, чтобы дойти до вызова `webpush`."""
    original_public, original_private = settings.vapid_public_key, settings.vapid_private_key
    settings.vapid_public_key = "test-public-key"
    settings.vapid_private_key = "test-private-key"
    yield
    settings.vapid_public_key, settings.vapid_private_key = original_public, original_private


def _make_response(status_code: int):
    class _Response:
        pass

    response = _Response()
    response.status_code = status_code
    return response


def test_invalid_subscription_removed_after_failed_send(db, make_user, vapid_configured):
    user = make_user(Module.CLIENTS)
    db.add(PushSubscription(user_id=user.id, endpoint="https://push.example/ep1", p256dh="p", auth="a"))
    db.commit()

    with patch(
        "app.notifications.push.webpush",
        side_effect=WebPushException("gone", response=_make_response(410)),
    ):
        notify(db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="t")

    remaining = db.query(PushSubscription).filter(PushSubscription.user_id == user.id).all()
    assert remaining == []


def test_server_error_on_send_keeps_subscription(db, make_user, vapid_configured):
    """500 от push-сервиса — не повод удалять подписку, только 404/410."""
    user = make_user(Module.CLIENTS)
    db.add(PushSubscription(user_id=user.id, endpoint="https://push.example/ep2", p256dh="p", auth="a"))
    db.commit()

    with patch(
        "app.notifications.push.webpush",
        side_effect=WebPushException("boom", response=_make_response(500)),
    ):
        notify(db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="t")

    remaining = db.query(PushSubscription).filter(PushSubscription.user_id == user.id).all()
    assert len(remaining) == 1


def test_create_and_delete_push_subscription_via_api(api, make_user, db):
    user = make_user(Module.CLIENTS)
    client = api(user)
    body = {"endpoint": "https://push.example/ep3", "keys": {"p256dh": "p", "auth": "a"}}

    response = client.post("/api/notifications/push-subscriptions", json=body)
    assert response.status_code == 204
    assert db.query(PushSubscription).filter(PushSubscription.endpoint == body["endpoint"]).count() == 1

    response = client.request(
        "DELETE", "/api/notifications/push-subscriptions", json={"endpoint": body["endpoint"]}
    )
    assert response.status_code == 204
    assert db.query(PushSubscription).filter(PushSubscription.endpoint == body["endpoint"]).count() == 0


def test_resubscribing_same_endpoint_updates_keys_not_duplicates(api, make_user, db):
    user = make_user(Module.CLIENTS)
    client = api(user)
    endpoint = "https://push.example/ep4"

    client.post(
        "/api/notifications/push-subscriptions",
        json={"endpoint": endpoint, "keys": {"p256dh": "old", "auth": "old"}},
    )
    client.post(
        "/api/notifications/push-subscriptions",
        json={"endpoint": endpoint, "keys": {"p256dh": "new", "auth": "new"}},
    )

    rows = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).all()
    assert len(rows) == 1
    assert rows[0].p256dh == "new"
