"""«Итоги дня» и «Итоги недели» на «Пульсе» (0027): что произошло за период,
а не «сколько сейчас» (как `service._snapshot_*`).

Считается прямо из дат существующих сущностей (`created_at`, `*_locked_at`,
`posted_at`, события статусов задач) — отдельного лога и таблицы нет. Истории
смены стадии клиента в БД нет, поэтому «сменили стадию» не считаем — вместо
этого берём отметки, у которых дата есть (документы/оплата/остаток
зафиксированы). Каждая метрика считается за текущий и предыдущий период той же
длины (день к вчера, неделя к прошлой неделе) — для сравнения на фронте.

Суммы и счётчики — обычный SQL по индексируемым датам, без ИИ, поэтому
отдельного кэша не заводим (в отличие от ИИ-блоков с `ai_cache`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.accounting.models import MoneyDirection, MoneyMovement, MoneyMovementStatus
from app.clients.models import BALANCE_TZ, Client, ClientNote
from app.common.module_access import Module
from app.dashboard.schemas import PeriodMetric, PeriodSection, PeriodSummaryOut
from app.dashboard.service import SECTION_LABELS
from app.installation.models import Installation
from app.marketing.models import ContentItem, ContentStage
from app.production.models import MaterialRequest, Production
from app.tasks.models import Task, TaskStageEvent, TaskStatus
from app.users.models import User
from app.warehouse.models import StockMovement, StockMovementReason, Supply

Period = Literal["day", "week"]

_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


@dataclass(frozen=True)
class Window:
    start: datetime  # включительно, UTC
    end: datetime  # исключительно, UTC

    @property
    def start_date(self) -> date:
        return self.start.astimezone(BALANCE_TZ).date()

    @property
    def end_date(self) -> date:
        return self.end.astimezone(BALANCE_TZ).date()


def _local_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=BALANCE_TZ).astimezone(timezone.utc)


def windows(period: Period, now: datetime | None = None) -> tuple[Window, Window]:
    """(текущий, предыдущий) по календарным суткам Москвы: день — сегодня и
    вчера; неделя — последние 7 суток, включая сегодня, и 7 суток до них."""
    today = (now or datetime.now(timezone.utc)).astimezone(BALANCE_TZ).date()
    length = 1 if period == "day" else 7
    start_day = today - timedelta(days=length - 1)
    current = Window(_local_midnight(start_day), _local_midnight(today + timedelta(days=1)))
    previous = Window(_local_midnight(start_day - timedelta(days=length)), current.start)
    return current, previous


def _between(column, w: Window):
    return (column >= w.start) & (column < w.end)


def _count(db: Session, column, w: Window, *extra) -> int:
    return db.query(func.count()).select_from(column.class_).filter(_between(column, w), *extra).scalar() or 0


# ------------------------------------------------------------ по разделам --
# Каждый сборщик отдаёт {ключ метрики: значение} за одно окно; подписи и порядок
# — в METRICS ниже.

def _clients(db: Session, w: Window) -> dict[str, float]:
    return {
        "new_clients": _count(db, Client.created_at, w),
        "documents_locked": _count(db, Client.documents_locked_at, w),
        "payment_locked": _count(db, Client.payment_locked_at, w),
        "balance_paid": _count(db, Client.balance_paid_at, w),
        "client_notes": _count(db, ClientNote.created_at, w),
    }


def _tasks(db: Session, w: Window) -> dict[str, float]:
    now = datetime.now(timezone.utc)
    closed = (
        db.query(func.count(func.distinct(TaskStageEvent.task_id)))
        .filter(_between(TaskStageEvent.created_at, w), TaskStageEvent.to_status == TaskStatus.DONE)
        .scalar()
        or 0
    )
    # Срок вышел в периоде, а задача так и не закрыта.
    overdue = (
        db.query(func.count(Task.id))
        .filter(_between(Task.deadline, w), Task.deadline < now, Task.status != TaskStatus.DONE)
        .scalar()
        or 0
    )
    return {"tasks_created": _count(db, Task.created_at, w), "tasks_closed": closed, "tasks_overdue": overdue}


def _production(db: Session, w: Window) -> dict[str, float]:
    return {
        "new_productions": _count(db, Production.created_at, w),
        "material_requests": _count(db, MaterialRequest.created_at, w),
    }


def _installation(db: Session, w: Window) -> dict[str, float]:
    scheduled = (
        db.query(func.count(Installation.id))
        .filter(Installation.scheduled_date >= w.start_date, Installation.scheduled_date < w.end_date)
        .scalar()
        or 0
    )
    return {"new_installations": _count(db, Installation.created_at, w), "installations_scheduled": scheduled}


_ISSUED = (
    StockMovementReason.ISSUED,
    StockMovementReason.ISSUED_TECHCARD,
    StockMovementReason.ISSUED_MANUAL,
)


def _warehouse(db: Session, w: Window) -> dict[str, float]:
    return {
        "supplies": _count(db, Supply.created_at, w),
        "stock_issued": _count(db, StockMovement.created_at, w, StockMovement.reason.in_(_ISSUED)),
        "stock_written_off": _count(
            db, StockMovement.created_at, w, StockMovement.reason == StockMovementReason.WRITE_OFF
        ),
    }


def _marketing(db: Session, w: Window) -> dict[str, float]:
    released = (
        db.query(func.count(ContentItem.id))
        .filter(
            ContentItem.stage.in_([ContentStage.RELEASE, ContentStage.ANALYSIS]),
            ContentItem.planned_release_date >= w.start_date,
            ContentItem.planned_release_date < w.end_date,
        )
        .scalar()
        or 0
    )
    return {"new_content": _count(db, ContentItem.created_at, w), "content_released": released}


def _accounting(db: Session, w: Window) -> dict[str, float]:
    posted = [_between(MoneyMovement.posted_at, w), MoneyMovement.status == MoneyMovementStatus.POSTED]

    def total(direction: MoneyDirection) -> float:
        return float(
            db.query(func.coalesce(func.sum(MoneyMovement.amount), 0))
            .filter(*posted, MoneyMovement.direction == direction)
            .scalar()
            or 0
        )

    return {
        "movements_posted": db.query(func.count(MoneyMovement.id)).filter(*posted).scalar() or 0,
        "income_rub": total(MoneyDirection.INCOME),
        "expense_rub": total(MoneyDirection.EXPENSE),
    }


# (ключ, подпись, единица, «рост — это плохо»)
Metric = tuple[str, str, str, bool]

SECTIONS: dict[str, tuple[Module, Callable[[Session, Window], dict[str, float]], list[Metric]]] = {
    "clients": (Module.CLIENTS, _clients, [
        ("new_clients", "Новых клиентов", "count", False),
        ("documents_locked", "Документы зафиксированы", "count", False),
        ("payment_locked", "Оплата зафиксирована", "count", False),
        ("balance_paid", "Остаток оплачен", "count", False),
        ("client_notes", "Заметок по клиентам", "count", False),
    ]),
    "production": (Module.PRODUCTION, _production, [
        ("new_productions", "Новых производств", "count", False),
        ("material_requests", "Заявок на материалы", "count", False),
    ]),
    "installation": (Module.INSTALLATION, _installation, [
        ("new_installations", "Новых монтажей", "count", False),
        ("installations_scheduled", "Монтажей по графику", "count", False),
    ]),
    "warehouse": (Module.WAREHOUSE, _warehouse, [
        ("supplies", "Поставок принято", "count", False),
        ("stock_issued", "Отпусков материалов", "count", False),
        ("stock_written_off", "Списаний", "count", True),
    ]),
    "marketing": (Module.MARKETING, _marketing, [
        ("new_content", "Новых материалов", "count", False),
        ("content_released", "Вышло по плану выпуска", "count", False),
    ]),
    "tasks": (Module.TASKS, _tasks, [
        ("tasks_created", "Создано задач", "count", False),
        ("tasks_closed", "Закрыто задач", "count", False),
        ("tasks_overdue", "Срок вышел, не закрыты", "count", True),
    ]),
    "accounting": (Module.ACCOUNTING, _accounting, [
        ("movements_posted", "Проведено проводок", "count", False),
        ("income_rub", "Поступления", "rub", False),
        ("expense_rub", "Расходы", "rub", False),
    ]),
}


def _label(period: Period, current: Window) -> tuple[str, str]:
    start = current.start_date
    end = current.end_date - timedelta(days=1)
    if period == "day":
        return f"Сегодня, {start.day} {_MONTHS[start.month - 1]}", "вчера"
    span = f"{start.day} {_MONTHS[start.month - 1]} — {end.day} {_MONTHS[end.month - 1]}"
    return f"Последние 7 дней, {span}", "предыдущие 7 дней"


def build_period_summary(db: Session, user: User, period: Period) -> PeriodSummaryOut:
    current, previous = windows(period)
    label, compare_label = _label(period, current)
    sections: list[PeriodSection] = []
    for key, (module, builder, metrics) in SECTIONS.items():
        if not user.has_access(module):
            continue
        now_values = builder(db, current)
        prev_values = builder(db, previous)
        sections.append(
            PeriodSection(
                section=key,
                title=SECTION_LABELS[key],
                metrics=[
                    PeriodMetric(
                        key=metric_key,
                        label=metric_label,
                        unit=unit,
                        value=now_values[metric_key],
                        previous=prev_values[metric_key],
                        growth_is_bad=growth_is_bad,
                    )
                    for metric_key, metric_label, unit, growth_is_bad in metrics
                ],
            )
        )
    return PeriodSummaryOut(
        period=period,
        label=label,
        compare_label=compare_label,
        start=current.start,
        end=current.end,
        generated_at=datetime.now(timezone.utc),
        sections=sections,
    )
