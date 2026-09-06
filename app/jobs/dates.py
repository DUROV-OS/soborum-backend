"""Единая трактовка «дня» для ежедневных задач: календарный день в
``settings.kb_timezone``. Ingest и саммари должны сходиться на одной дате и
одном окне, поэтому логика тут одна на всех.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings


def kb_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.kb_timezone)
    except Exception:  # noqa: BLE001 — кривой конфиг не должен ронять задачу
        return ZoneInfo("UTC")


def yesterday(tz: ZoneInfo | None = None) -> date:
    tz = tz or kb_tz()
    return (datetime.now(tz) - timedelta(days=1)).date()


def day_bounds_utc(day: date, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """[00:00, 24:00) указанного календарного дня в kb-таймзоне, в UTC —
    в этих границах хранится TelegramMessage.sent_at."""
    tz = tz or kb_tz()
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    from datetime import timezone as _utc

    return start_local.astimezone(_utc.utc), end_local.astimezone(_utc.utc)


def iso(day: date) -> str:
    return day.isoformat()


def as_kb_local(dt: datetime, tz: ZoneInfo | None = None) -> datetime:
    """Привести момент к kb-таймзоне. Наивный datetime (так его отдаёт
    SQLite) считаем UTC — именно в UTC его пишет приёмник апдейтов."""
    from datetime import timezone as _utc

    tz = tz or kb_tz()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_utc.utc)
    return dt.astimezone(tz)
