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
from app.accounting.models import Counterparty, Organization
from app.sales.models import SalesOrder
from app.sales.schemas import CreateShipment, PostShipment
from app.sales.service import create_shipment, post_shipment, shipped
from app.warehouse.models import WarehouseMaterial, Warehouse, MaterialCategory
from app.warehouse.reservations import create as reserve, Create
from app.warehouse.reservation_service import available


@pytest.mark.skipif(
    not os.getenv("LOCK_TEST_DATABASE_URL"), reason="requires isolated PostgreSQL"
)
def test_sales_share_order_and_material_locks_with_reservations():
    url = make_url(os.environ["LOCK_TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql"
    assert (
        url.host in {"localhost", "127.0.0.1"}
        or url.database == "soborbum_sales_check_20261029"
    )
    schema = "sales_test_" + uuid4().hex
    root = create_engine(url)
    with root.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-c search_path={schema} -c lock_timeout=10000"}
    )
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            actor = User(
                email="sales@example.test",
                full_name="Проверка",
                hashed_password="not-a-login",
                role=UserRole.ADMIN,
            )
            cp, org = Counterparty(name="Покупатель"), Organization(
                name="Организация", short_name="Тест"
            )
            m = WarehouseMaterial(
                title="Брус",
                code="PG-SALES",
                warehouse=Warehouse.TECHNOLOGY,
                category=MaterialCategory.NONE,
                unit="шт",
                quantity_in_stock=6,
                purchase_price=40,
            )
            db.add_all([actor, cp, org, m])
            db.flush()
            lines = [
                {
                    "material_id": m.id,
                    "title": m.title,
                    "unit": m.unit,
                    "quantity": "5",
                    "unit_price": "100",
                    "line_total": "500",
                    "stock_tracked": True,
                    "opening_shipped": "0",
                }
            ]
            orders = [
                SalesOrder(
                    number=str(i),
                    status="confirmed",
                    counterparty_id=cp.id,
                    organization_id=org.id,
                    lines=lines,
                    total=500,
                    actor_id=actor.id,
                    occurred_at=datetime.now(timezone.utc),
                )
                for i in range(2)
            ]
            db.add_all(orders)
            db.commit()
            uid, mid, ids = actor.id, m.id, [o.id for o in orders]
            drafts = [
                create_shipment(
                    db,
                    CreateShipment(
                        request_id=uuid4(),
                        order_id=ids[0],
                        order_version=1,
                        occurred_at=datetime.now(timezone.utc),
                        received_by="Цех",
                        lines=[{"order_line": 0, "quantity": "4"}],
                    ),
                    actor,
                )
                for _ in range(2)
            ]
        gate = threading.Barrier(2)

        def post_one(draft):
            with Session(engine) as db:
                actor = db.get(User, uid)
                gate.wait(timeout=10)
                try:
                    post_shipment(
                        db,
                        draft["id"],
                        PostShipment(request_id=uuid4(), version=1, order_version=1),
                        actor,
                    )
                    return 200
                except HTTPException as e:
                    db.rollback()
                    return e.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(timeout=30) for f in [pool.submit(post_one, d) for d in drafts]
            ]
        assert sorted(results) == [200, 409]
        with Session(engine) as db:
            assert db.get(WarehouseMaterial, mid).quantity_in_stock == 2
            assert shipped(db, db.get(SalesOrder, ids[0]))[0] == 4
            # Restore physical stock only inside the isolated fixture, so the
            # next race tests different orders sharing one warehouse material.
            db.get(WarehouseMaterial, mid).quantity_in_stock = 6
            db.commit()
            draft = create_shipment(
                db,
                CreateShipment(
                    request_id=uuid4(),
                    order_id=ids[1],
                    order_version=1,
                    occurred_at=datetime.now(timezone.utc),
                    received_by="Цех",
                    lines=[{"order_line": 0, "quantity": "4"}],
                ),
                db.get(User, uid),
            )
            third = SalesOrder(
                number="third",
                status="confirmed",
                counterparty_id=cp.id,
                organization_id=org.id,
                lines=lines,
                total=500,
                actor_id=uid,
                occurred_at=datetime.now(timezone.utc),
            )
            db.add(third)
            db.commit()
            third_id = third.id
        gate = threading.Barrier(2)

        def reserve_other():
            with Session(engine) as db:
                actor = db.get(User, uid)
                gate.wait(timeout=10)
                try:
                    reserve(
                        Create(
                            request_id=uuid4(),
                            material_id=mid,
                            quantity=4,
                            sales_order_id=third_id,
                            sales_line=0,
                        ),
                        db,
                        actor,
                    )
                    return 200
                except HTTPException as e:
                    db.rollback()
                    return e.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(timeout=30)
                for f in [pool.submit(post_one, draft), pool.submit(reserve_other)]
            ]
        assert sorted(results) == [200, 409]
        with Session(engine) as db:
            assert available(db, db.get(WarehouseMaterial, mid)) == 2
    finally:
        engine.dispose()
        with root.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()
