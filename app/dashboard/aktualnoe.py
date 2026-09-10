"""Блок «Актуальное» на «Сегодня»: три цикла клиента, над которыми, по оценке
ИИ, активнее всего работали в последнее время.

Активность собирается из фактов, которые уже есть в БД (не из внешнего MAX):
заметки по клиенту, отметки о фиксации стадий, события статусов связанных
задач. По топ-3 циклам ИИ проставляет процент выполненности текущей стадии и
фразу из 2-3 слов. Результат кешируется на 12 часов (см. app.ai.cache).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
from app.common.module_access import Module
from app.core.config import settings
from app.dashboard.schemas import AktualnoeItem, AktualnoeOut
from app.users.models import User

from app.clients.models import Client, ClientNote, ClientStage
from app.cycle.models import Cycle, CycleStatus
from app.installation.models import InstallationStage
from app.production.models import Production, ProductionModule
from app.tasks.models import Task, TaskLinkType, TaskStageEvent

RECENT_WINDOW = timedelta(days=21)
CACHE_KEY = "dashboard_aktualnoe"
CACHE_TTL = timedelta(hours=12)
TOP_N = 3

# Человеческое название текущей стадии + запасной процент, если ИИ недоступен.
CLIENT_STAGE_LABEL: dict[ClientStage, tuple[str, int]] = {
    ClientStage.LEAD: ("Первичный контакт", 10),
    ClientStage.DISCUSSION: ("Обсуждение проекта", 30),
    ClientStage.APPROVAL: ("Согласование документов", 55),
    ClientStage.PAYMENT: ("Ожидание оплаты", 75),
    ClientStage.POSTPAYMENT: ("Оплата после получения", 90),
}
INSTALLATION_STAGE_LABEL: dict[InstallationStage, tuple[str, int]] = {
    InstallationStage.DELIVERY: ("Доставка дома", 70),
    InstallationStage.INSTALLATION: ("Монтаж на участке", 85),
    InstallationStage.FOLLOWUP: ("Проработка после монтажа", 95),
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass
class CycleActivity:
    cycle_id: int
    client_name: str
    stage_label: str
    fallback_percent: int
    last_activity: datetime
    recent_events: int
    signals: list[str] = field(default_factory=list)

    @property
    def days_since_activity(self) -> int:
        return max(0, (datetime.now(timezone.utc) - self.last_activity).days)


def _stage_of(cycle: Cycle, client: Client) -> tuple[str, int]:
    if cycle.status == CycleStatus.COMPLETED:
        return "Цикл завершён", 100
    if cycle.status == CycleStatus.INSTALLATION and cycle.installation is not None:
        return INSTALLATION_STAGE_LABEL.get(cycle.installation.stage, ("Монтаж", 80))
    if cycle.status == CycleStatus.PRODUCTION:
        return "Производство дома", 50
    return CLIENT_STAGE_LABEL.get(client.stage, ("Работа с клиентом", 20))


def _task_activity(db: Session, cycle: Cycle, client: Client) -> tuple[datetime | None, int, int]:
    """Последнее событие статуса и число недавних событий по задачам цикла:
    задачи модулей производства этого цикла + задачи стадий клиента."""
    module_ids = [
        m_id
        for (m_id,) in db.query(ProductionModule.id)
        .join(Production, Production.id == ProductionModule.production_id)
        .filter(Production.cycle_id == cycle.id)
        .all()
    ]
    q = db.query(TaskStageEvent).join(Task, Task.id == TaskStageEvent.task_id)
    conditions = [(Task.link_type == TaskLinkType.CLIENT_STAGE) & (Task.link_id == client.id)]
    if module_ids:
        conditions.append(Task.module_id.in_(module_ids))
    q = q.filter(or_(*conditions))

    last = q.with_entities(func.max(TaskStageEvent.created_at)).scalar()
    since = datetime.now(timezone.utc) - RECENT_WINDOW
    recent = q.filter(TaskStageEvent.created_at >= since).count()
    total = q.count()
    return _aware(last), recent, total


def collect_cycle_activity(db: Session) -> list[CycleActivity]:
    """Все циклы с клиентом, отсортированные по свежести работы над ними."""
    now = datetime.now(timezone.utc)
    since = now - RECENT_WINDOW

    # последняя заметка по каждому клиенту
    last_note: dict[int, datetime] = {
        client_id: _aware(ts)
        for client_id, ts in db.query(ClientNote.client_id, func.max(ClientNote.created_at))
        .group_by(ClientNote.client_id)
        .all()
    }
    recent_note_count: dict[int, int] = {
        client_id: cnt
        for client_id, cnt in db.query(ClientNote.client_id, func.count(ClientNote.id))
        .filter(ClientNote.created_at >= since)
        .group_by(ClientNote.client_id)
        .all()
    }

    out: list[CycleActivity] = []
    for cycle in db.query(Cycle).all():
        client = cycle.client
        if client is None:
            continue

        stage_label, fallback_percent = _stage_of(cycle, client)

        stamps: list[tuple[str, datetime]] = []
        for name, value in (
            ("создан", client.created_at),
            ("проект зафиксирован", client.project_locked_at),
            ("документы зафиксированы", client.documents_locked_at),
            ("оплата зафиксирована", client.payment_locked_at),
            ("остаток оплачен", client.balance_paid_at),
        ):
            aware = _aware(value)
            if aware is not None:
                stamps.append((name, aware))

        note_ts = last_note.get(client.id)
        if note_ts is not None:
            stamps.append(("заметка по клиенту", note_ts))

        task_ts, task_recent, task_total = _task_activity(db, cycle, client)
        if task_ts is not None:
            stamps.append(("событие по задаче", task_ts))

        if not stamps:
            continue

        signal_name, last_activity = max(stamps, key=lambda s: s[1])
        recent_events = recent_note_count.get(client.id, 0) + task_recent

        out.append(
            CycleActivity(
                cycle_id=cycle.id,
                client_name=client.full_name,
                stage_label=stage_label,
                fallback_percent=fallback_percent,
                last_activity=last_activity,
                recent_events=recent_events,
                signals=[f"{name}: {ts.date().isoformat()}" for name, ts in sorted(stamps, key=lambda s: s[1], reverse=True)][:5]
                + ([f"событий по задачам всего: {task_total}"] if task_total else []),
            )
        )

    out.sort(key=lambda a: (a.recent_events, a.last_activity), reverse=True)
    return out


def top_active_cycles(db: Session, limit: int = 3) -> list[CycleActivity]:
    return collect_cycle_activity(db)[:limit]


# --------------------------------------------------------------- ИИ-оценка --

SUBMIT_TOOL_NAME = "submit_aktualnoe"

SYSTEM_PROMPT = (
    "Ты — аналитик системы управления производством модульных домов «Soborbum». "
    "Тебе передан список из нескольких циклов клиентов с их текущей стадией и "
    "признаками недавней работы над ними (даты заметок, фиксаций стадий, событий "
    "по задачам). По КАЖДОМУ переданному циклу оцени, насколько выполнена его "
    "ТЕКУЩАЯ стадия.\n\n"
    "Правила:\n"
    "- percent — целое 0..100, насколько текущая стадия близка к завершению. "
    "Опирайся на переданные признаки, не выдумывай факты.\n"
    "- phrase — 2-3 слова по-русски о том, что сейчас происходит на этой стадии "
    "(например: «согласуют планировку», «ждут предоплату», «собирают модули»).\n"
    "- stage — короткое (2-3 слова) название текущей стадии по-русски.\n"
    "- Верни ровно по одному объекту на каждый cycle_id из входных данных.\n"
    "- Отвечай ТОЛЬКО вызовом инструмента submit_aktualnoe, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить оценку выполненности текущей стадии по каждому циклу.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "cycle_id": {"type": "integer"},
                        "stage": {"type": "string"},
                        "percent": {"type": "integer", "minimum": 0, "maximum": 100},
                        "phrase": {"type": "string"},
                    },
                    "required": ["cycle_id", "stage", "percent", "phrase"],
                },
            }
        },
        "required": ["items"],
    },
}


def ai_rate_cycles(activities: list[CycleActivity]) -> dict[int, dict] | None:
    """Спросить у Claude процент/фразу/стадию по каждому циклу. `None` — если
    ИИ не настроен или не вернул валидный ответ (вызывающий откатывается на
    детерминированные значения)."""
    if not settings.anthropic_api_key or not activities:
        return None

    from app.core.llm import anthropic_client

    payload = [
        {
            "cycle_id": a.cycle_id,
            "client": a.client_name,
            "current_stage": a.stage_label,
            "days_since_last_activity": a.days_since_activity,
            "recent_events": a.recent_events,
            "signals": a.signals,
        }
        for a in activities
    ]

    try:
        response = anthropic_client().messages.create(
            model=settings.ai_model,
            max_tokens=1024,
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

    out: dict[int, dict] = {}
    for row in tool_use.input.get("items", []):
        try:
            cid = int(row["cycle_id"])
            percent = max(0, min(100, int(row["percent"])))
        except (KeyError, TypeError, ValueError):
            continue
        stage = str(row.get("stage") or "").strip()
        phrase = str(row.get("phrase") or "").strip()
        out[cid] = {"percent": percent, "stage": stage, "phrase": phrase}
    return out or None


# ---------------------------------------------------------------- сборка --

def _build(db: Session) -> AktualnoeOut:
    activities = top_active_cycles(db, TOP_N)
    now = datetime.now(timezone.utc)
    if not activities:
        return AktualnoeOut(generated_at=now, items=[], ai_configured=bool(settings.anthropic_api_key), degraded=False)

    rated = ai_rate_cycles(activities)
    degraded = rated is None
    items: list[AktualnoeItem] = []
    for a in activities:
        r = (rated or {}).get(a.cycle_id, {})
        items.append(
            AktualnoeItem(
                cycle_id=a.cycle_id,
                client_name=a.client_name,
                stage=r.get("stage") or a.stage_label,
                percent=r.get("percent", a.fallback_percent),
                phrase=r.get("phrase", ""),
            )
        )
    return AktualnoeOut(
        generated_at=now,
        items=items,
        ai_configured=bool(settings.anthropic_api_key),
        degraded=degraded,
    )


def generate_aktualnoe(db: Session, user: User, force: bool = False) -> AktualnoeOut:
    """Блок «Актуальное» для «Сегодня». Пустой список, если у сотрудника нет
    доступа к разделу «Цикл клиента». Кеш на 12 часов, общий на организацию;
    кнопка «Обновить» на «Сегодня» проходит как force=True."""
    if not user.has_access(Module.CYCLE):
        return AktualnoeOut(generated_at=datetime.now(timezone.utc), items=[])

    cached = ai_cache.get(db, CACHE_KEY, force, ttl=CACHE_TTL)
    if cached is not None:
        return AktualnoeOut(**cached)

    result = _build(db)
    # Не кешируем деградированный ответ — чтобы после появления ключа он
    # пересчитался в ближайший запрос, а не жил 12 часов.
    if not result.degraded:
        ai_cache.set(db, CACHE_KEY, result.model_dump(mode="json"), result.generated_at)
    return result
