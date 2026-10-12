"""0049: перевод технического текста ошибки в фразу до 10 слов."""

import pytest

from app.ai import error_humanizer as mod
from app.common.module_access import Module


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    mod._cache.clear()
    mod._calls.clear()
    monkeypatch.setattr(mod.settings, "openai_api_key", "test-key")


def _fake_model(monkeypatch, answer):
    calls = []

    def ask(text, status):
        calls.append((text, status))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(mod, "_ask_model", ask)
    return calls


def test_sanitize_hides_emails_and_long_numbers_and_truncates():
    text = mod.sanitize("Клиент ivan@example.com, тел +7 (900) 123-45-67 не найден " + "x" * 500)

    assert "ivan@example.com" not in text
    assert "123-45-67" not in text
    assert "<email>" in text and "<number>" in text
    assert len(text) <= mod.MAX_INPUT_CHARS


def test_clean_answer_strips_quotes_and_rejects_long_or_empty():
    assert mod.clean_answer("«Проверьте заполнение полей».") == "Проверьте заполнение полей"
    assert mod.clean_answer("Первая строка\nвторая") == "Первая строка"
    assert mod.clean_answer("") is None
    assert mod.clean_answer("один два три четыре пять шесть семь восемь девять десять одиннадцать") is None


def test_humanize_returns_model_phrase_and_caches(monkeypatch):
    calls = _fake_model(monkeypatch, "Заполните обязательные поля")

    assert mod.humanize("Field required", 422, 1) == "Заполните обязательные поля"
    assert mod.humanize("Field required", 422, 2) == "Заполните обязательные поля"
    assert len(calls) == 1


def test_humanize_is_none_without_openai_key(monkeypatch):
    calls = _fake_model(monkeypatch, "не должно вызываться")
    monkeypatch.setattr(mod.settings, "openai_api_key", "")

    assert mod.humanize("Field required", 422, 1) is None
    assert calls == []


def test_humanize_is_none_when_model_fails_and_failure_not_cached(monkeypatch):
    _fake_model(monkeypatch, RuntimeError("quota"))
    assert mod.humanize("Internal Server Error", 500, 1) is None

    _fake_model(monkeypatch, "Сервер не справился, повторите позже")
    assert mod.humanize("Internal Server Error", 500, 1) == "Сервер не справился, повторите позже"


def test_humanize_rate_limited_per_user(monkeypatch):
    _fake_model(monkeypatch, "Повторите позже")

    for i in range(mod.RATE_LIMIT_PER_MINUTE):
        assert mod.humanize(f"error {i}", 500, 1) == "Повторите позже"
    assert mod.humanize("one more", 500, 1) is None
    assert mod.humanize("one more", 500, 2) == "Повторите позже"


def test_endpoint_returns_phrase_for_signed_in_user(api, make_user, monkeypatch):
    _fake_model(monkeypatch, "Повторите позже")
    user = make_user(Module.CLIENTS)

    response = api(user).post("/api/ai/humanize-error", json={"text": "Bad Gateway", "status": 502})

    assert response.status_code == 200
    assert response.json() == {"text": "Повторите позже"}


def test_endpoint_returns_null_text_on_failure(api, make_user, monkeypatch):
    _fake_model(monkeypatch, RuntimeError("down"))
    user = make_user(Module.CLIENTS)

    response = api(user).post("/api/ai/humanize-error", json={"text": "Bad Gateway", "status": 502})

    assert response.status_code == 200
    assert response.json() == {"text": None}
