"""Какая модель Claude и с каким усилием обслуживает какую задачу.

Одно место вместо `settings.ai_model`, размазанного по всему коду. Каждый
профиль → готовые kwargs для `messages.create()` / `beta.messages.create()`:
`model` и, если у профиля задано усилие, `output_config={"effort": ...}`.

Раскладка по умолчанию:

| Профиль        | Модель          | effort  | Где используется                                   |
|----------------|-----------------|---------|---------------------------------------------------|
| CHAT           | claude-sonnet-5 | —       | чат Марины (app/ai/engine.py)                     |
| QUICK          | claude-haiku-4-5| — (без extended thinking) | блоки-резюме, аналитика разделов, выбор актуальных задач |
| BOARD_LEAD     | claude-opus-5   | medium  | синтез и правки совета директоров, «актуализация» |
| BOARD_AGENT    | claude-sonnet-5 | —       | 7 ролей-агентов совета, исследовательская справка |
| DAILY_JOB      | claude-sonnet-5 | low     | ежедневные фоновые задачи (app/jobs/daily.py)     |
| WEEKLY_JOB     | claude-sonnet-5 | medium  | еженедельные фоновые задачи (app/jobs/weekly.py)  |

Haiku 4.5 не принимает `output_config.effort` и работает без extended
thinking, поэтому у QUICK усилие всегда пустое.

Переопределяется через .env (см. `settings.ai_model` и `settings.ai_models`).
"""

from __future__ import annotations

import enum
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_AI_MODEL = "claude-sonnet-5"
_VALID_EFFORT = {"low", "medium", "high", "xhigh", "max"}
# Модели, которым нельзя слать output_config.effort.
_NO_EFFORT_MODELS = {"claude-haiku-4-5", "claude-haiku-4-5-20251001"}


class ModelProfile(str, enum.Enum):
    CHAT = "chat"
    QUICK = "quick"
    BOARD_LEAD = "board_lead"
    BOARD_AGENT = "board_agent"
    DAILY_JOB = "daily_job"
    WEEKLY_JOB = "weekly_job"


# (model, effort | None)
_DEFAULTS: dict[ModelProfile, tuple[str, str | None]] = {
    ModelProfile.CHAT: ("claude-sonnet-5", None),
    ModelProfile.QUICK: ("claude-haiku-4-5", None),
    ModelProfile.BOARD_LEAD: ("claude-opus-5", "medium"),
    ModelProfile.BOARD_AGENT: ("claude-sonnet-5", None),
    ModelProfile.DAILY_JOB: ("claude-sonnet-5", "low"),
    ModelProfile.WEEKLY_JOB: ("claude-sonnet-5", "medium"),
}


def _parse_overrides(raw: str) -> dict[ModelProfile, tuple[str | None, str | None]]:
    """`chat=claude-opus-5,daily_job=:high` → {CHAT: ("claude-opus-5", None),
    DAILY_JOB: (None, "high")}. Кривые куски пропускаются с предупреждением."""
    out: dict[ModelProfile, tuple[str | None, str | None]] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            logger.warning("AI_MODELS: пропущен элемент без '=': %r", chunk)
            continue
        key, value = chunk.split("=", 1)
        try:
            profile = ModelProfile(key.strip().lower())
        except ValueError:
            logger.warning("AI_MODELS: неизвестный профиль %r", key.strip())
            continue
        model_part, _, effort_part = value.strip().partition(":")
        model = model_part.strip() or None
        effort = effort_part.strip().lower() or None
        if effort and effort not in _VALID_EFFORT:
            logger.warning("AI_MODELS[%s]: неизвестный effort %r, игнорирую", profile.value, effort)
            effort = None
        out[profile] = (model, effort)
    return out


def _resolve(profile: ModelProfile) -> tuple[str, str | None]:
    model, effort = _DEFAULTS[profile]

    # Глобальный рубильник: нестандартный AI_MODEL переопределяет модель везде.
    if settings.ai_model and settings.ai_model != _DEFAULT_AI_MODEL:
        model = settings.ai_model

    ov_model, ov_effort = _parse_overrides(settings.ai_models).get(profile, (None, None))
    if ov_model:
        model = ov_model
    if ov_effort:
        effort = ov_effort

    if effort and model in _NO_EFFORT_MODELS:
        effort = None
    return model, effort


def profile_params(profile: ModelProfile) -> dict:
    """kwargs для messages.create(): `{"model": ...}` и, если нужно,
    `{"output_config": {"effort": ...}}`."""
    model, effort = _resolve(profile)
    params: dict = {"model": model}
    if effort:
        params["output_config"] = {"effort": effort}
    return params


def profile_model(profile: ModelProfile) -> str:
    """Только id модели — для мест, где kwargs собирается вручную."""
    return _resolve(profile)[0]
