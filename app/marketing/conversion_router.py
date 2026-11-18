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
from app.marketing import conversion_delivery

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
    if user.role == UserRole.ADMIN:
        result['delivery']['batches'] = conversion_delivery.summary(db, include_batches=True)['batches']
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


class DeliveryControlInput(BaseModel):
    enabled: bool


class DeliveryAcknowledgement(BaseModel):
    expected_version: int = Field(ge=1)
    status: Literal['uploading', 'submitted', 'processed', 'rejected', 'uncertain']
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    upload_id: str | None = Field(default=None, pattern=r'^[0-9]{1,80}$')
    matched_rows: int | None = Field(default=None, ge=0)
    note: str = Field(default='', max_length=500)


@router.post('/conversion-delivery/control')
def configure_delivery(payload: DeliveryControlInput, db: Session = Depends(get_db),
                       user: User = Depends(require_admin)):
    result = conversion_delivery.configure(db, payload.enabled, f'admin:{user.id}')
    db.commit()
    return result


@router.post('/conversion-delivery/batches')
def prepare_delivery(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    result = conversion_delivery.prepare(db, f'admin:{user.id}')
    db.commit()
    return result


@router.get('/conversion-delivery/batches/{batch_id}/file')
def download_delivery(batch_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return conversion_delivery.file(db, batch_id)


@router.post('/conversion-delivery/batches/{batch_id}/acknowledge')
def acknowledge_delivery(batch_id: int, payload: DeliveryAcknowledgement,
                         db: Session = Depends(get_db), user: User = Depends(require_admin)):
    result = conversion_delivery.acknowledge(db, batch_id, actor=f'admin:{user.id}', **payload.model_dump())
    db.commit()
    return result


@router.get('/economics')
def get_economics(date_from: date | None = None, date_to: date | None = None,
                  db: Session = Depends(get_db),
                  user: User = Depends(require_view(Module.MARKETING)),
                  _clients: User = Depends(require_view(Module.CLIENTS)),
                  _accounting: User = Depends(require_view(Module.ACCOUNTING)),
                  _costing: User = Depends(require_view(Module.COSTING))):
    from app.marketing.economics import report
    from app.costing.service import require_cost
    require_cost(user)
    today = datetime.now(ZoneInfo('Europe/Moscow')).date()
    return report(db, date_from or today.replace(day=1), date_to or today)
