"""Стартовый мок-набор проводок «Бухгалтерии» (app.accounting.seed) — 0011-j.

Идемпотентность и переиспользование существующих сущностей: сидер не плодит
дубли на повторный вызов и привязывает проводки к реальным клиенту /
сотруднику / поставке, а при их отсутствии — пропускает такие строки.
"""

from app.accounting.models import MoneyMovement, MoneyMovementStatus, MoneySourceKind, MoneySubkind
from app.accounting.seed import ensure_accounting_seed
from app.clients import service as client_service
from app.clients.schemas import ClientCreate
from app.core.config import settings
from app.warehouse.models import Supply


def _client(db):
    return client_service.create_client(
        db, ClientCreate(full_name="Иван Сидоров", phone="+70000000000", email="i@example.com")
    )


def test_seed_populates_all_subkinds_and_statuses(db, make_user):
    admin = make_user(admin=True)
    worker = make_user()
    client = _client(db)
    supply = Supply(supplier_name="ООО Лес", created_by_id=admin.id)
    db.add(supply)
    db.commit()

    created = ensure_accounting_seed(db)
    assert created >= 10

    rows = db.query(MoneyMovement).all()
    assert {r.subkind for r in rows} == set(MoneySubkind)
    assert {r.status for r in rows} == set(MoneyMovementStatus)

    # привязки — к реально существующим сущностям
    sale = next(r for r in rows if r.subkind is MoneySubkind.SALE_INCOME)
    assert sale.source_kind is MoneySourceKind.CLIENT and sale.client_id == client.id
    salary = next(r for r in rows if r.subkind is MoneySubkind.SALARY_PAYOUT)
    assert salary.source_kind is MoneySourceKind.EMPLOYEE and salary.employee_id == worker.id
    supply_pay = next(r for r in rows if r.subkind is MoneySubkind.SUPPLY_PAYMENT)
    assert supply_pay.source_kind is MoneySourceKind.SUPPLY and supply_pay.supply_id == supply.id

    posted = [r for r in rows if r.status is MoneyMovementStatus.POSTED]
    assert posted and all(r.posted_at is not None for r in posted)
    cancelled = next(r for r in rows if r.status is MoneyMovementStatus.CANCELLED)
    assert cancelled.cancel_reason
    assert all(r.initiator_id == admin.id for r in rows)


def test_seed_is_idempotent(db, make_user):
    make_user(admin=True)
    first = ensure_accounting_seed(db)
    assert first > 0
    before = db.query(MoneyMovement).count()

    assert ensure_accounting_seed(db) == 0
    assert db.query(MoneyMovement).count() == before


def test_seed_skips_rows_without_their_source(db, make_user):
    # только админ: нет отдельного сотрудника-воркера, нет клиента, нет поставки
    make_user(admin=True)
    created = ensure_accounting_seed(db)
    rows = db.query(MoneyMovement).all()

    assert created == len(rows)
    # проводки, требующие клиента или поставки, пропущены
    assert not any(r.subkind is MoneySubkind.SALE_INCOME for r in rows)
    assert not any(r.subkind is MoneySubkind.SUPPLY_PAYMENT for r in rows)
    # зарплата ложится на админа (fallback), «прочее»/налог/аренда — есть
    assert any(r.subkind is MoneySubkind.SALARY_PAYOUT for r in rows)
    assert any(r.subkind is MoneySubkind.TAX for r in rows)


def test_seed_noop_without_admin(db):
    assert ensure_accounting_seed(db) == 0
    assert db.query(MoneyMovement).count() == 0


def test_seed_does_not_run_in_prod(db, make_user, monkeypatch):
    """Критично: это мок-данные для локальной разработки/приёмки, никогда не
    для боевой базы — на свежем проде реестр должен остаться пустым."""
    make_user(admin=True)
    monkeypatch.setattr(settings, "app_env", "prod")
    assert ensure_accounting_seed(db) == 0
    assert db.query(MoneyMovement).count() == 0
