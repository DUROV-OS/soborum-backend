from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

WidgetTone = Literal["neutral", "brand", "success", "warning", "danger", "info"]


class DashboardWidget(BaseModel):
    section: str
    title: str
    value: str
    hint: str | None = None
    tone: WidgetTone = "neutral"


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
