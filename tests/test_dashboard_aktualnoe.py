"""Блок «Актуальное» на «Сегодня» (app.dashboard.aktualnoe):

- без ANTHROPIC_API_KEY отдаётся деградированная подборка (топ по свежести,
  детерминированные проценты) и не кешируется;
- с «ИИ» результат кешируется и не пересчитывается чаще раза в 12 часов,
  а `reload=True` пересчитывает сразу;
- сотруднику без доступа к разделу «Цикл клиента» блок пустой.
"""

from datetime import timedelta

import pytest

from app.ai import cache as ai_cache
from app.ai.models import AiCacheEntry
from app.clients import service as client_service
from app.clients.models import ClientNote
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.dashboard import aktualnoe as mod
from app.dashboard.aktualnoe import CACHE_KEY, generate_aktualnoe


def _client_with_activity(db, name, notes):
    client = client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )
    for text in notes:
        db.add(ClientNote(client_id=client.id, author_id=1, text=text))
    db.commit()
    return client


@pytest.fixture
def user_cycle(make_user):
    return make_user(Module.CYCLE)


def test_degrades_and_is_not_cached_without_ai(db, user_cycle):
    _client_with_activity(db, "Альфа", ["звонок", "смета"])
    _client_with_activity(db, "Бета", ["письмо"])

    result = generate_aktualnoe(db, user_cycle)

    assert result.degraded is True
    assert result.ai_configured is False
    assert 1 <= len(result.items) <= 3
    for item in result.items:
        assert 0 <= item.percent <= 100
        assert item.stage
    # деградированный ответ не кешируется
    assert ai_cache.get(db, CACHE_KEY, ttl=timedelta(hours=12)) is None


def test_cache_not_recomputed_within_12h(db, user_cycle, monkeypatch):
    _client_with_activity(db, "Гамма", ["монтаж согласован"])

    calls = {"n": 0}

    def fake_rate(activities):
        calls["n"] += 1
        return {a.cycle_id: {"percent": 42, "stage": "тест-стадия", "phrase": "идут работы"} for a in activities}

    monkeypatch.setattr(mod, "ai_rate_cycles", fake_rate)

    first = generate_aktualnoe(db, user_cycle)
    assert first.degraded is False
    assert calls["n"] == 1
    assert first.items[0].percent == 42

    # второй запрос в пределах 12 часов — из кеша, ИИ не дёргаем
    second = generate_aktualnoe(db, user_cycle)
    assert calls["n"] == 1
    assert second.generated_at == first.generated_at

    # reload=True — пересчёт немедленно
    third = generate_aktualnoe(db, user_cycle, force=True)
    assert calls["n"] == 2
    assert third.generated_at >= first.generated_at

    # запись в кеше «состарилась» больше чем на 12 часов — снова пересчёт
    entry = db.get(AiCacheEntry, CACHE_KEY)
    entry.generated_at = entry.generated_at - timedelta(hours=13)
    db.commit()
    generate_aktualnoe(db, user_cycle)
    assert calls["n"] == 3


def test_empty_without_cycle_access(db, make_user):
    _client_with_activity(db, "Дельта", ["звонок"])
    user = make_user(Module.CLIENTS)

    result = generate_aktualnoe(db, user)

    assert result.items == []
