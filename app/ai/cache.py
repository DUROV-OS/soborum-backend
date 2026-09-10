"""TTL cache for AI-generated answers that are expensive to recompute but
cheap to reuse for a while - section analytics, task priorities and the
"Сегодня" dashboard today (app/ai/analytics.py, app/ai/priorities.py,
app/dashboard/service.py). Each GET recomputes via Claude only if the
cached entry is missing, stale, or the caller passed `reload=true` (wired
through from the frontend's "Обновить" button); otherwise it's served
straight from ai_cache_entries.

Not for chat turns - those are already conversation history, not a cacheable
snapshot answer.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai.models import AiCacheEntry

TTL = timedelta(hours=4)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def get(db: Session, key: str, force: bool = False, ttl: timedelta | None = None) -> dict | None:
    if force:
        return None
    entry = db.get(AiCacheEntry, key)
    if entry is None or datetime.now(timezone.utc) - _aware(entry.generated_at) > (ttl or TTL):
        return None
    return entry.payload


def set(db: Session, key: str, payload: dict, generated_at: datetime) -> None:
    entry = db.get(AiCacheEntry, key)
    if entry is None:
        entry = AiCacheEntry(key=key, payload=payload, generated_at=generated_at)
        db.add(entry)
    else:
        entry.payload = payload
        entry.generated_at = generated_at
    db.commit()
