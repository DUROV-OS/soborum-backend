"""Раскладка «задача → модель/усилие» и её переопределение через .env."""

import pytest

from app.ai import model_profiles as mp
from app.ai.model_profiles import ModelProfile as P
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    # По умолчанию — чистый конфиг без переопределений.
    monkeypatch.setattr(settings, "ai_model", "claude-sonnet-5", raising=False)
    monkeypatch.setattr(settings, "ai_models", "", raising=False)
    yield


def test_defaults_match_spec():
    assert mp.profile_params(P.CHAT) == {"model": "claude-sonnet-5"}
    assert mp.profile_params(P.QUICK) == {"model": "claude-haiku-4-5"}
    assert mp.profile_params(P.BOARD_LEAD) == {
        "model": "claude-opus-5",
        "output_config": {"effort": "medium"},
    }
    assert mp.profile_params(P.BOARD_AGENT) == {"model": "claude-sonnet-5"}
    assert mp.profile_params(P.DAILY_JOB) == {
        "model": "claude-sonnet-5",
        "output_config": {"effort": "low"},
    }
    assert mp.profile_params(P.WEEKLY_JOB) == {
        "model": "claude-sonnet-5",
        "output_config": {"effort": "medium"},
    }


def test_quick_never_gets_effort_even_if_forced(monkeypatch):
    monkeypatch.setattr(settings, "ai_models", "quick=claude-haiku-4-5:max", raising=False)
    assert mp.profile_params(P.QUICK) == {"model": "claude-haiku-4-5"}


def test_global_ai_model_overrides_every_profile_model(monkeypatch):
    monkeypatch.setattr(settings, "ai_model", "claude-opus-4-8", raising=False)
    assert mp.profile_params(P.CHAT)["model"] == "claude-opus-4-8"
    assert mp.profile_params(P.QUICK)["model"] == "claude-opus-4-8"
    # усилие профиля сохраняется
    assert mp.profile_params(P.DAILY_JOB)["output_config"] == {"effort": "low"}


def test_per_profile_override_model_and_effort(monkeypatch):
    monkeypatch.setattr(
        settings, "ai_models", "chat=claude-opus-5:high,daily_job=:xhigh", raising=False
    )
    assert mp.profile_params(P.CHAT) == {
        "model": "claude-opus-5",
        "output_config": {"effort": "high"},
    }
    # пустая модель → дефолт профиля, поменялся только effort
    assert mp.profile_params(P.DAILY_JOB) == {
        "model": "claude-sonnet-5",
        "output_config": {"effort": "xhigh"},
    }


def test_bad_override_chunks_are_ignored(monkeypatch):
    monkeypatch.setattr(
        settings, "ai_models", "garbage,unknown_profile=x,chat=claude-opus-5:loud", raising=False
    )
    # неизвестный effort отбрасывается, модель применяется
    assert mp.profile_params(P.CHAT) == {"model": "claude-opus-5"}


def test_per_profile_override_beats_global(monkeypatch):
    monkeypatch.setattr(settings, "ai_model", "claude-opus-4-8", raising=False)
    monkeypatch.setattr(settings, "ai_models", "board_agent=claude-sonnet-5", raising=False)
    assert mp.profile_params(P.BOARD_AGENT)["model"] == "claude-sonnet-5"
    assert mp.profile_params(P.CHAT)["model"] == "claude-opus-4-8"
