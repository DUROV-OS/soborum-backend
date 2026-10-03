"""0089-a: AI_PROVIDER picks Claude or ChatGPT; keys for both live side by side."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_default_provider_is_anthropic_so_existing_env_keeps_working():
    s = _settings(anthropic_api_key="sk-ant-x", ai_model="claude-sonnet-5")
    assert s.ai_provider == "anthropic"
    assert s.llm_configured is True
    assert s.llm_model == "claude-sonnet-5"


def test_openai_provider_uses_openai_key_and_model():
    s = _settings(ai_provider="openai", anthropic_api_key="sk-ant-x", openai_api_key="sk-oa", openai_model="gpt-5")
    assert s.llm_configured is True
    assert s.llm_model == "gpt-5"


def test_active_provider_without_its_key_is_not_configured():
    # A Claude key alone does not make ChatGPT usable, and vice versa.
    assert _settings(ai_provider="openai", anthropic_api_key="sk-ant-x").llm_configured is False
    assert _settings(ai_provider="anthropic", openai_api_key="sk-oa").llm_configured is False


@pytest.mark.parametrize("raw, expected", [(" OpenAI ", "openai"), ("ANTHROPIC", "anthropic"), ("", "anthropic")])
def test_provider_value_is_normalized(raw, expected):
    assert _settings(ai_provider=raw).ai_provider == expected


def test_unknown_provider_fails_loudly():
    with pytest.raises(ValidationError):
        _settings(ai_provider="chatgpt")
