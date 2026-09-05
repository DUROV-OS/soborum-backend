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
