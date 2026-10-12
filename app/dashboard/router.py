from typing import Literal

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.dashboard import aktualnoe as aktualnoe_service
from app.dashboard import missed as missed_service
from app.dashboard import overview as overview_service
from app.dashboard import period_summary as period_summary_service
from app.dashboard import service as dashboard_service
from app.dashboard.schemas import AktualnoeOut, MissedOut, PeriodSummaryOut, SectionSignalOut, TodayDashboardOut
from app.db.session import get_db
from app.users.models import User

app = FastAPI(
    title="Soborbum — Сегодня",
    description="Актуальные показатели и очередь внимания из доступных сотруднику разделов. Работает без AI-провайдера.",
    version="0.1.1",
)

@app.get("/today", response_model=TodayDashboardOut)
def get_today(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return dashboard_service.generate_widgets(db, user, force=reload)


@app.get("/today/section/{section}", response_model=SectionSignalOut)
def get_section_signal(
    section: str, reload: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Раздел «Работы» — реальный сигнал по одному разделу, отдельным
    запросом (не держит остальные плитки) и с кешем на 6 часов."""
    return overview_service.generate_section_signal(db, user, section, force=reload)


@app.get("/today/summary/{period}", response_model=PeriodSummaryOut)
def get_period_summary(
    period: Literal["day", "week"], db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """«Итоги дня»/«Итоги недели»: что произошло за период по разделам, к
    которым у сотрудника есть доступ, и то же за предыдущий период."""
    return period_summary_service.build_period_summary(db, user, period)


@app.get("/aktualnoe", response_model=AktualnoeOut)
def get_aktualnoe(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Три цикла клиента, над которыми активнее всего работали в последнее
    время. Кеш обновляется раз в 12 часов; `reload=true` пересчитывает сразу."""
    return aktualnoe_service.generate_aktualnoe(db, user, force=reload)


@app.get("/missed", response_model=MissedOut)
def get_missed(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Блок «Вы могли пропустить»: 2–3 самых важных непрочитанных уведомления
    по оценке ИИ. Кеш на час на пользователя; `reload=true` пересчитывает сразу."""
    return missed_service.generate_missed(db, user, force=reload)
