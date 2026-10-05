"""app.ai.story_points (0070-d): оценка идёт через активного провайдера ИИ
(llm_client, с 0089 — ChatGPT), а не напрямую в Claude; без ключа активного
провайдера и при ошибке вызова — None, задача создаётся без оценки."""

from types import SimpleNamespace

import pytest

from app.ai import story_points
from app.core import llm


class _FakeToolUse:
    type = "tool_use"

    def __init__(self, input_):
        self.input = input_


def _fake_client(calls, points):
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=[_FakeToolUse({"story_points": points})])

    return SimpleNamespace(messages=SimpleNamespace(create=create))


@pytest.fixture
def openai_provider(monkeypatch):
    monkeypatch.setattr(story_points.settings, "ai_provider", "openai")
    monkeypatch.setattr(story_points.settings, "openai_api_key", "fake-key-for-tests")
    monkeypatch.setattr(story_points.settings, "openai_model", "gpt-test")
    monkeypatch.setattr(story_points.settings, "anthropic_api_key", "")


def test_estimate_goes_through_active_provider(openai_provider, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(llm, "llm_client", lambda **_: _fake_client(calls, 5))
    monkeypatch.setattr(llm, "anthropic_client", lambda **_: pytest.fail("Claude вызван в обход llm_client"))

    assert story_points.estimate_story_points("Собрать каркас", None, "production") == 5
    assert calls[0]["model"] == "gpt-test"


def test_estimate_none_without_active_provider_key(monkeypatch):
    monkeypatch.setattr(story_points.settings, "ai_provider", "openai")
    monkeypatch.setattr(story_points.settings, "openai_api_key", "")
    monkeypatch.setattr(story_points.settings, "anthropic_api_key", "sk-ant-x")
    monkeypatch.setattr(llm, "llm_client", lambda **_: pytest.fail("без ключа ИИ не вызывается"))

    assert story_points.estimate_story_points("Задача", None, "manual") is None


def test_estimate_none_on_out_of_scale_answer_or_error(openai_provider, monkeypatch):
    monkeypatch.setattr(llm, "llm_client", lambda **_: _fake_client([], 4))
    assert story_points.estimate_story_points("Задача", None, "manual") is None

    def broken(**_):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(llm, "llm_client", broken)
    assert story_points.estimate_story_points("Задача", None, "manual") is None
