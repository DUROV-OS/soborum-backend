from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.notifications.models import NotificationKind

WidgetTone = Literal["neutral", "brand", "success", "warning", "danger", "info"]


class DashboardWidget(BaseModel):
    section: str
    title: str
    value: str
    hint: str | None = None
    tone: WidgetTone = "neutral"
    # Куда ведёт клик по плитке, если не просто в раздел: например счётчик
    # задач открывает доску с тем же набором (0084-h).
    href: str | None = None


class DashboardAction(BaseModel):
    id: str
    section: str
    title: str
    description: str
    href: str
    count: int
    tone: WidgetTone = "warning"


class TodayDashboardOut(BaseModel):
    generated_at: datetime
    summary: str
    widgets: list[DashboardWidget]
    actions: list[DashboardAction] = Field(default_factory=list)
    source: Literal["database"] = "database"
    ai_configured: bool = False


class SectionSignalOut(BaseModel):
    """Один раздел «Работы» — то же действие, что попало бы в `actions` у
    `TodayDashboardOut`, отдельным кэшируемым запросом (см. app.dashboard.overview).

    `checked` — по разделу вообще есть проверка в ATTENTION (список того, что
    считается сигналом внимания): true, если `action is None` значит «реально
    посчитали и проблем нет», false — если раздел просто не входит в ATTENTION
    (или нет доступа/раздел неизвестен) и `action is None` не означает вообще
    ничего, кроме «не проверяли»."""

    section: str
    action: DashboardAction | None = None
    checked: bool = False
    # Текст «что именно проверили и что там чисто» — заполнен только когда
    # checked=true и action=None (см. ALL_CLEAR_TEXT в overview.py).
    clear_text: str | None = None
    generated_at: datetime


class AktualnoeItem(BaseModel):
    cycle_id: int
    client_name: str
    stage: str
    percent: int = Field(ge=0, le=100)
    phrase: str = ""


class AktualnoeOut(BaseModel):
    generated_at: datetime
    items: list[AktualnoeItem] = Field(default_factory=list)
    ai_configured: bool = False
    # true — блок собран без ИИ (топ по свежести, проценты детерминированные)
    degraded: bool = False


class PeriodMetric(BaseModel):
    key: str
    label: str
    # count — штуки, rub — рубли (форматирует фронт)
    unit: Literal["count", "rub"] = "count"
    value: float
    previous: float
    # рост показателя — плохой знак (просрочки, списания): фронт красит наоборот
    growth_is_bad: bool = False


class PeriodSection(BaseModel):
    section: str
    title: str
    metrics: list[PeriodMetric]


class PeriodSummaryOut(BaseModel):
    """«Итоги дня»/«Итоги недели» (0027): метрики по доступным разделам за
    текущий период и за предыдущий такой же длины (для сравнения)."""

    period: Literal["day", "week"]
    label: str
    compare_label: str
    start: datetime
    end: datetime
    generated_at: datetime
    sections: list[PeriodSection] = Field(default_factory=list)


class MissedItem(BaseModel):
    """Непрочитанное уведомление в блоке «Вы могли пропустить» (0027) — поля
    уведомления плюс короткое пояснение ИИ, почему оно важно."""

    id: int
    kind: NotificationKind
    title: str
    body: str | None = None
    object_type: str | None = None
    object_id: int | None = None
    created_at: datetime
    reason: str = ""


class MissedOut(BaseModel):
    generated_at: datetime
    items: list[MissedItem] = Field(default_factory=list)
    ai_configured: bool = False
    # true — отбор без ИИ (по приоритету повода и свежести)
    degraded: bool = False
