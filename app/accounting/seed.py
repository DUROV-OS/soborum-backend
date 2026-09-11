"""Стартовый мок-набор проводок «Бухгалтерии». Идемпотентно (skip, если в
`money_movements` уже что-то есть) и **никогда не запускается в prod** — это
демо-данные для локальной разработки/приёмки, не для боевой базы (см.
`app.tasks.demo_seed`/`app.clients.demo_seed`, тот же принцип), по образцу
`app.board.seed.ensure_seed` и `app.users.service.bootstrap_admin`: делает
что-то ровно один раз после первого деплоя, no-op на каждом рестарте.

Ничего не создаёт ради привязок — только переиспурует уже существующих
клиентов / сотрудников / поставки. Если подходящей сущности нет, проводка,
которой она нужна, пропускается (набор ужимается, не падает).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.accounting.models import (
    INCOME_SUBKINDS,
    MoneyAssessment,
    MoneyDirection,
    MoneyMovement,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
)


def _direction_for(subkind: MoneySubkind) -> MoneyDirection:
    return MoneyDirection.INCOME if subkind in INCOME_SUBKINDS else MoneyDirection.EXPENSE
from app.clients.models import Client
from app.users.models import User, UserRole
from app.warehouse.models import Supply

_NOW = datetime.now(timezone.utc)

# (subkind, status, amount, tax, source_kind, payment_purpose, comment, cancel_reason, days_ago)
_SPECS: list[tuple] = [
    (MoneySubkind.SALE_INCOME, MoneyMovementStatus.POSTED, 1_850_000, 308_333, MoneySourceKind.CLIENT,
     "аванс по договору поставки дома", None, None, 21),
    (MoneySubkind.SALE_INCOME, MoneyMovementStatus.APPROVED, 1_200_000, 200_000, MoneySourceKind.CLIENT,
     "второй платёж по договору", None, None, 6),
    (MoneySubkind.SALE_INCOME, MoneyMovementStatus.DRAFT, 640_000, 106_667, MoneySourceKind.CLIENT,
     "остаток после получения дома", None, None, 1),
    (MoneySubkind.SALARY_PAYOUT, MoneyMovementStatus.POSTED, 420_000, 0, MoneySourceKind.EMPLOYEE,
     "зарплата за август", "выплата", None, 12),
    (MoneySubkind.SALARY_PAYOUT, MoneyMovementStatus.APPROVED, 445_000, 0, MoneySourceKind.EMPLOYEE,
     "зарплата за сентябрь", "утверждено к выплате", None, 2),
    (MoneySubkind.SALARY_PAYOUT, MoneyMovementStatus.DRAFT, 60_000, 0, MoneySourceKind.EMPLOYEE,
     "премия по итогам монтажа", "начислено", None, 1),
    (MoneySubkind.SUPPLY_PAYMENT, MoneyMovementStatus.POSTED, 512_400, 85_400, MoneySourceKind.SUPPLY,
     "оплата поставки бруса и доски", None, None, 15),
    (MoneySubkind.SUPPLY_PAYMENT, MoneyMovementStatus.DRAFT, 190_000, 31_667, MoneySourceKind.SUPPLY,
     "предоплата за метизы и утеплитель", None, None, 1),
    (MoneySubkind.TAX, MoneyMovementStatus.POSTED, 274_000, 0, MoneySourceKind.NONE,
     "НДС за 2 квартал", None, None, 30),
    (MoneySubkind.RENT, MoneyMovementStatus.POSTED, 130_000, 21_667, MoneySourceKind.NONE,
     "аренда цеха за сентябрь", None, None, 9),
    (MoneySubkind.OTHER_INCOME, MoneyMovementStatus.DRAFT, 45_000, 0, MoneySourceKind.NONE,
     "возврат от поставщика за брак", None, None, 3),
    (MoneySubkind.OTHER_EXPENSE, MoneyMovementStatus.CANCELLED, 27_000, 0, MoneySourceKind.NONE,
     "оплата вывоза мусора", None, "дубль — уже оплачено наличными", 4),
]


def ensure_accounting_seed(db: Session) -> int:
    """Возвращает число созданных проводок (0 в prod, если реестр уже был не
    пуст, или если не хватило сущностей для привязок)."""
    if settings.is_prod:
        return 0
    if db.query(MoneyMovement).first() is not None:
        return 0

    admin = db.query(User).filter(User.role == UserRole.ADMIN).order_by(User.id).first()
    if admin is None:
        return 0

    client = db.query(Client).order_by(Client.id).first()
    employee = (
        db.query(User).filter(User.role == UserRole.WORKER).order_by(User.id).first()
        or admin
    )
    supply = db.query(Supply).order_by(Supply.id).first()

    source_ok = {
        MoneySourceKind.NONE: True,
        MoneySourceKind.CLIENT: client is not None,
        MoneySourceKind.EMPLOYEE: employee is not None,
        MoneySourceKind.SUPPLY: supply is not None,
    }

    created = 0
    for subkind, mstatus, amount, tax, source_kind, purpose, comment, cancel_reason, days_ago in _SPECS:
        if not source_ok[source_kind]:
            continue
        moment = _NOW - timedelta(days=days_ago)
        mm = MoneyMovement(
            direction=_direction_for(subkind),
            subkind=subkind,
            amount=amount,
            currency="RUB",
            tax=tax,
            assessment=MoneyAssessment.ACTUAL,
            affects_profit=True,
            initiator_id=admin.id,
            status=mstatus,
            posted_at=moment if mstatus is MoneyMovementStatus.POSTED else None,
            cancel_reason=cancel_reason,
            payment_purpose=purpose,
            comment=comment,
            source_kind=source_kind,
            client_id=client.id if source_kind is MoneySourceKind.CLIENT else None,
            employee_id=employee.id if source_kind is MoneySourceKind.EMPLOYEE else None,
            supply_id=supply.id if source_kind is MoneySourceKind.SUPPLY else None,
            created_at=moment,
        )
        db.add(mm)
        created += 1

    if created:
        db.commit()
    return created
