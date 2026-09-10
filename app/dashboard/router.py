from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.dashboard import aktualnoe as aktualnoe_service
from app.dashboard import service as dashboard_service
from app.dashboard.schemas import AktualnoeOut, TodayDashboardOut
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


@app.get("/aktualnoe", response_model=AktualnoeOut)
def get_aktualnoe(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Три цикла клиента, над которыми активнее всего работали в последнее
    время. Кеш обновляется раз в 12 часов; `reload=true` пересчитывает сразу."""
    return aktualnoe_service.generate_aktualnoe(db, user, force=reload)
