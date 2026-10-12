"""Блок «Вы могли пропустить» на «Пульсе» (0027): 2–3 самых важных из
непрочитанных уведомлений сотрудника — по оценке ИИ.

Кандидаты — только свои непрочитанные уведомления за последние 14 дней (не
больше 30 свежих). ИИ выбирает самые важные и коротко (до 8 слов) объясняет,
почему. Если ИИ не настроен или не ответил — детерминированный отбор по
приоритету повода и свежести (`degraded=true`, не кешируется). Результат
кешируется на час на пользователя и пересчитывается раньше срока, если одно
из показанных уведомлений уже прочитано.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
from app.core.config import settings
from app.dashboard.schemas import MissedItem, MissedOut
from app.notifications.models import Notification, NotificationKind
from app.users.models import User

LOOKBACK = timedelta(days=14)
POOL_SIZE = 30
MAX_ITEMS = 3
CACHE_TTL = timedelta(hours=1)

# Приоритет повода, когда ИИ недоступен: просрочка и деньги важнее обновлений.
KIND_PRIORITY: dict[NotificationKind, int] = {
    NotificationKind.TASK_OVERDUE: 100,
    NotificationKind.MONEY_MOVEMENT_UPDATE: 80,
    NotificationKind.MATERIAL_REQUEST_UPDATE: 70,
    NotificationKind.TASK_DUE: 60,
    NotificationKind.CLIENT_UPDATE: 50,
    NotificationKind.PRODUCTION_UPDATE: 50,
    NotificationKind.INSTALLATION_UPDATE: 50,
    NotificationKind.MAX_MESSAGE: 40,
    NotificationKind.FEEDBACK_UPDATE: 40,
    NotificationKind.CONTENT_UPDATE: 30,
}


def cache_key(user_id: int) -> str:
    return f"dashboard_missed_{user_id}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def unread_pool(db: Session, user_id: int) -> list[Notification]:
    since = datetime.now(timezone.utc) - LOOKBACK
    return (
        db.query(Notification)
        .filter(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            Notification.created_at >= since,
        )
        .order_by(Notification.id.desc())
        .limit(POOL_SIZE)
        .all()
    )


def fallback_pick(pool: list[Notification]) -> list[Notification]:
    ranked = sorted(
        pool,
        key=lambda n: (KIND_PRIORITY.get(n.kind, 0), _aware(n.created_at)),
        reverse=True,
    )
    return ranked[:MAX_ITEMS]


# --------------------------------------------------------------- ИИ-отбор --

SUBMIT_TOOL_NAME = "submit_missed"

SYSTEM_PROMPT = (
    "Ты — помощник руководителя производства модульных домов «Soborbum». Тебе "
    "передан список НЕПРОЧИТАННЫХ уведомлений сотрудника. Выбери из них не более "
    f"{MAX_ITEMS} самых важных, которые он рискует пропустить и которые требуют "
    "его действий или влияют на деньги, сроки и клиентов.\n\n"
    "Правила:\n"
    "- Выбирай только id из входных данных, не выдумывай.\n"
    "- Просрочки и сорванные сроки, деньги, нехватка материалов и жалобы клиентов "
    "важнее рядовых обновлений. Если важного нет — верни меньше трёх или пустой список.\n"
    "- reason — до 8 слов по-русски: почему это стоит открыть сейчас. Только то, "
    "что следует из текста уведомления, без домыслов.\n"
    "- Порядок в ответе — от самого важного.\n"
    "- Отвечай ТОЛЬКО вызовом инструмента submit_missed, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить самые важные непрочитанные уведомления.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
                    "required": ["id", "reason"],
                },
            }
        },
        "required": ["items"],
    },
}


def ai_pick(pool: list[Notification]) -> list[tuple[Notification, str]] | None:
    """Выбор ИИ: список (уведомление, причина) от важного к менее важному.
    `None` — ИИ не настроен или не вернул пригодный ответ."""
    if not settings.llm_configured or not pool:
        return None

    from app.core.llm import llm_client

    now = datetime.now(timezone.utc)
    payload = [
        {
            "id": n.id,
            "kind": n.kind.value,
            "title": n.title,
            "body": n.body,
            "hours_ago": max(0, int((now - _aware(n.created_at)).total_seconds() // 3600)),
        }
        for n in pool
    ]
    try:
        response = llm_client().messages.create(
            model=settings.llm_model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
        )
    except Exception:  # noqa: BLE001 — сеть/квоты/парсинг: молча деградируем
        return None

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        return None

    by_id = {n.id: n for n in pool}
    picked: list[tuple[Notification, str]] = []
    for row in tool_use.input.get("items", []):
        try:
            notification = by_id.get(int(row["id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if notification is None or any(n.id == notification.id for n, _ in picked):
            continue
        picked.append((notification, str(row.get("reason") or "").strip()))
    # Пустой ответ при непустом пуле — это «ИИ решил, что важного нет»; но
    # показываем всё же запасной отбор, чтобы блок не пропадал при сбое разбора.
    return picked[:MAX_ITEMS] or None


# ---------------------------------------------------------------- сборка --

def _item(notification: Notification, reason: str) -> MissedItem:
    return MissedItem(
        id=notification.id,
        kind=notification.kind,
        title=notification.title,
        body=notification.body,
        object_type=notification.object_type,
        object_id=notification.object_id,
        created_at=notification.created_at,
        reason=reason,
    )


def _from_cache(db: Session, user: User, cached: dict) -> MissedOut | None:
    """Показанные из кеша уведомления, если все ещё непрочитаны; иначе `None`
    (кеш устарел — пользователь успел открыть часть из них)."""
    reasons: dict[int, str] = {int(k): v for k, v in cached.get("reasons", {}).items()}
    ids = cached.get("ids", [])
    rows = {
        n.id: n
        for n in db.query(Notification).filter(
            Notification.user_id == user.id, Notification.id.in_(ids)
        )
    }
    if any(i not in rows or rows[i].read_at is not None for i in ids):
        return None
    return MissedOut(
        generated_at=cached["generated_at"],
        items=[_item(rows[i], reasons.get(i, "")) for i in ids],
        ai_configured=settings.llm_configured,
        degraded=False,
    )


def generate_missed(db: Session, user: User, force: bool = False) -> MissedOut:
    """Блок «Вы могли пропустить». Уведомления принадлежат получателю, поэтому
    доступ к разделам не проверяем — как в самом API уведомлений."""
    now = datetime.now(timezone.utc)

    cached = ai_cache.get(db, cache_key(user.id), force, ttl=CACHE_TTL)
    if cached is not None:
        result = _from_cache(db, user, cached)
        if result is not None:
            return result

    pool = unread_pool(db, user.id)
    if not pool:
        return MissedOut(generated_at=now, items=[], ai_configured=settings.llm_configured)

    picked = ai_pick(pool)
    degraded = picked is None
    if picked is None:
        picked = [(n, "") for n in fallback_pick(pool)]

    result = MissedOut(
        generated_at=now,
        items=[_item(n, reason) for n, reason in picked],
        ai_configured=settings.llm_configured,
        degraded=degraded,
    )
    # Деградированный ответ не кешируем — после появления ключа пересчитается
    # в ближайший запрос (как в «Актуальном»).
    if not degraded:
        ai_cache.set(
            db,
            cache_key(user.id),
            {
                "generated_at": now.isoformat(),
                "ids": [n.id for n, _ in picked],
                "reasons": {str(n.id): reason for n, reason in picked},
            },
            now,
        )
    return result
