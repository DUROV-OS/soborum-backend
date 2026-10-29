import os, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.db import import_all_models
from app.db.base import Base
from app.users.models import User, UserRole
from app.warehouse.models import (
    WarehouseMaterial,
    Warehouse,
    MaterialCategory,
    Supplier,
    StockMovement,
)
from app.accounting.models import (
    SupplierOrder,
    Organization,
    BankAccount,
    MoneyMovement,
)
from app.purchasing.models import PurchaseReceipt, PurchaseReturn
from app.purchasing.returns import ReturnCreate, create, refund
from app.purchasing.schemas import Payment


@pytest.mark.skipif(
    not os.getenv("LOCK_TEST_DATABASE_URL"), reason="requires isolated PostgreSQL"
)
def test_return_and_cash_refund_capacity_are_serialized():
    url = make_url(os.environ["LOCK_TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql"
    assert (
        url.host in {"localhost", "127.0.0.1"}
        or url.database == "soborbum_returns_check_20261029"
    )
    schema = "purchase_return_test_" + uuid4().hex
    root = create_engine(url)
    with root.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-c search_path={schema} -c lock_timeout=10000"}
    )
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            actor = User(
                email="return@example.test",
                full_name="Проверка возвратов",
                hashed_password="not-a-login",
                role=UserRole.ADMIN,
            )
            supplier = Supplier(name="Тест возвратов", categories=[], contacts=[])
            material = WarehouseMaterial(
                warehouse=Warehouse.TECHNOLOGY,
                category=MaterialCategory.NONE,
                title="Брус",
                code="RETURN-PG",
                unit="шт",
                quantity_in_stock=10,
                purchase_price=20,
            )
            org = Organization(name="Тестовая организация", short_name="Тест")
            db.add_all([actor, supplier, material, org])
            db.flush()
            account = BankAccount(
                organization_id=org.id,
                name="Тестовый счёт",
                currency="RUB",
                is_active=True,
            )
            order = SupplierOrder(
                supplier_id=supplier.id,
                items=[
                    {
                        "material": "Брус",
                        "quantity": 10,
                        "received_quantity": 10,
                        "warehouse_material_id": material.id,
                    }
                ],
                total_cost=200,
            )
            db.add_all([account, order])
            db.flush()
            receipt = PurchaseReceipt(
                supplier_id=supplier.id,
                order_id=order.id,
                actor_id=actor.id,
                number="R1",
                status="posted",
                occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                lines=[
                    {
                        "material_id": material.id,
                        "title": "Брус",
                        "unit": "шт",
                        "quantity": 10,
                        "unit_price": 20,
                        "stock_tracked": True,
                    }
                ],
            )
            db.add(receipt)
            db.commit()
            aid, mid, rid, account_id = actor.id, material.id, receipt.id, account.id
        gate = threading.Barrier(2)

        def returning():
            with Session(engine) as db:
                owner = db.get(User, aid)
                gate.wait(timeout=10)
                try:
                    row = create(
                        ReturnCreate(
                            request_id=uuid4(),
                            receipt_id=rid,
                            occurred_at=datetime.now(timezone.utc),
                            note="Параллельный возврат",
                            lines=[{"receipt_line": 0, "quantity": 7}],
                        ),
                        db,
                        owner,
                    )
                    return ("ok", row["id"])
                except HTTPException as e:
                    db.rollback()
                    return (e.status_code, None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(timeout=30) for f in [pool.submit(returning) for _ in range(2)]
            ]
        assert sorted(str(r[0]) for r in results) == ["409", "ok"]
        retid = next(r[1] for r in results if r[0] == "ok")
        with Session(engine) as db:
            assert db.get(WarehouseMaterial, mid).quantity_in_stock == 3
            assert (
                db.query(PurchaseReturn).count() == 1
                and db.query(StockMovement).count() == 1
            )
        gate = threading.Barrier(2)

        def refunding():
            with Session(engine) as db:
                owner = db.get(User, aid)
                gate.wait(timeout=10)
                try:
                    refund(
                        retid,
                        Payment(request_id=uuid4(), amount=100, account_id=account_id),
                        db,
                        owner,
                    )
                    return 201
                except HTTPException as e:
                    db.rollback()
                    return e.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(
                f.result(timeout=30) for f in [pool.submit(refunding) for _ in range(2)]
            ) == [201, 409]
        with Session(engine) as db:
            assert db.query(MoneyMovement).count() == 1
    finally:
        engine.dispose()
        with root.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()
