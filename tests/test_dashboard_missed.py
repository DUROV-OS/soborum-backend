"""Блок «Вы могли пропустить» на «Пульсе» (0027): выбор самых важных из
непрочитанных уведомлений, запасной отбор без ИИ, кеш и инвалидация."""

from datetime import datetime, timedelta, timezone

from app.ai import cache as ai_cache
from app.common.module_access import Module
from app.dashboard import missed as mod
from app.notifications.models import Notification, NotificationKind
from app.notifications.service import mark_read, notify


def _notify(db, user, kind, title, **kw):
    return notify(db, user.id, kind=kind, module=Module.TASKS, title=title, **kw)


def test_empty_when_nothing_unread(api, make_user, db):
    user = make_user(Module.TASKS)

    body = api(user).get("/api/dashboard/missed").json()

    assert body["items"] == []


def test_fallback_picks_top_three_by_kind_priority_without_ai(api, make_user, db):
    user = make_user(Module.TASKS)
    _notify(db, user, NotificationKind.CONTENT_UPDATE, "Пост обновлён")
    _notify(db, user, NotificationKind.CLIENT_UPDATE, "Клиент обновлён")
    _notify(db, user, NotificationKind.TASK_OVERDUE, "Задача просрочена")
    _notify(db, user, NotificationKind.MONEY_MOVEMENT_UPDATE, "Проводка проведена")

    body = api(user).get("/api/dashboard/missed").json()

    assert body["degraded"] is True
    assert [i["title"] for i in body["items"]] == ["Задача просрочена", "Проводка проведена", "Клиент обновлён"]
    assert ai_cache.get(db, mod.cache_key(user.id), ttl=mod.CACHE_TTL) is None


def test_only_own_unread_recent_notifications(api, make_user, db):
    user = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    read = _notify(db, user, NotificationKind.TASK_OVERDUE, "Прочитано")
    mark_read(db, read)
    _notify(db, other, NotificationKind.TASK_OVERDUE, "Чужое")
    old = _notify(db, user, NotificationKind.TASK_OVERDUE, "Старое")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    db.commit()
    _notify(db, user, NotificationKind.CLIENT_UPDATE, "Моё свежее")

    body = api(user).get("/api/dashboard/missed").json()

    assert [i["title"] for i in body["items"]] == ["Моё свежее"]


def test_ai_choice_is_used_cached_and_invalidated_when_read(api, make_user, db, monkeypatch):
    user = make_user(Module.TASKS)
    first = _notify(db, user, NotificationKind.CLIENT_UPDATE, "Первое")
    second = _notify(db, user, NotificationKind.CLIENT_UPDATE, "Второе")
    _notify(db, user, NotificationKind.CLIENT_UPDATE, "Третье")
    calls = {"n": 0}

    def fake_pick(pool):
        calls["n"] += 1
        by_id = {n.id: n for n in pool}
        return [(by_id[i], reason) for i, reason in ((second.id, "срок горит"), (first.id, "ждёт ответа")) if i in by_id]

    monkeypatch.setattr(mod, "ai_pick", fake_pick)
    client = api(user)

    body = client.get("/api/dashboard/missed").json()
    assert body["degraded"] is False
    assert [(i["title"], i["reason"]) for i in body["items"]] == [("Второе", "срок горит"), ("Первое", "ждёт ответа")]

    client.get("/api/dashboard/missed")
    assert calls["n"] == 1  # второй запрос — из кеша

    client.post(f"/api/notifications/{second.id}/read")
    body = client.get("/api/dashboard/missed").json()
    assert calls["n"] == 2  # одно из показанных прочитано — пересчёт
    assert second.id not in [i["id"] for i in body["items"]]


def test_ai_cannot_return_foreign_or_unknown_ids(db, make_user, monkeypatch):
    user = make_user(Module.TASKS)
    mine = _notify(db, user, NotificationKind.CLIENT_UPDATE, "Моё")
    pool = mod.unread_pool(db, user.id)

    class Block:
        type = "tool_use"
        input = {"items": [{"id": 999999, "reason": "выдумка"}, {"id": mine.id, "reason": "реальное"}]}

    class Response:
        content = [Block()]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**_):
                return Response()

    import app.core.llm as llm

    monkeypatch.setattr(llm, "llm_client", lambda: FakeClient)
    monkeypatch.setattr(mod.settings.__class__, "llm_configured", property(lambda self: True))

    picked = mod.ai_pick(pool)

    assert [(n.id, reason) for n, reason in picked] == [(mine.id, "реальное")]
