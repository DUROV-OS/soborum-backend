from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from app.common.module_access import Module, AccessLevel
from app.core.deps import require_view, require_edit, require_admin
from app.db.session import get_db
from app.users.models import User, UserRole
from app.marketing import conversions

router = APIRouter()


class QualityReview(BaseModel):
    expected_version: int = Field(ge=0)
    outcome: Literal['unreviewed', 'qualified', 'unqualified', 'spam', 'duplicate']
    reason: str = Field(min_length=10, max_length=1000)
    contact_confirmed: bool = False
    need_confirmed: bool = False
    region_confirmed: bool = False

    @model_validator(mode='after')
    def confirmed(self):
        self.reason = self.reason.strip()
        if len(self.reason) < 10:
            raise ValueError('Кратко запишите фактическое основание оценки')
        if self.outcome == 'qualified' and not all((self.contact_confirmed, self.need_confirmed, self.region_confirmed)):
            raise ValueError('Подтвердите контакт, потребность и возможность строительства в согласованных регионах')
        return self


@router.get('/conversions')
def get_conversions(date_from: date | None = None, date_to: date | None = None,
                    db: Session = Depends(get_db), user: User = Depends(require_view(Module.MARKETING))):
    today = datetime.now(ZoneInfo('Europe/Moscow')).date()
    result = conversions.report(db, date_from or today.replace(day=1), date_to or today,
        can_review=user.access_level(Module.CLIENTS) >= AccessLevel.EDIT,
        can_open_clients=user.has_access(Module.CLIENTS))
    return {**result, 'can_export': user.role == UserRole.ADMIN}


@router.post('/conversions/{request_id}/review')
def record_quality(request_id: int, payload: QualityReview, db: Session = Depends(get_db),
                   user: User = Depends(require_edit(Module.CLIENTS)),
                   _: User = Depends(require_view(Module.MARKETING))):
    result = conversions.review_request(db, request_id, payload, user)
    db.commit()
    return result


@router.get('/conversions-export')
def prepare_export(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return conversions.export_csv(db)
