import os, threading
from concurrent.futures import ThreadPoolExecutor
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
    WarehouseOperationKind,
    IssueDestinationKind,
)
from app.warehouse.reservations import create, release, Create, Change
from app.warehouse.reservation_service import available
from app.warehouse.reservation_models import StockReservation
from app.warehouse.service import create_stock_operation


@pytest.mark.skipif(
    not os.getenv("LOCK_TEST_DATABASE_URL"), reason="requires isolated PostgreSQL"
)
def test_holds_and_physical_issues_share_row_locks():
    url = make_url(os.environ["LOCK_TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql"
    assert (
        url.host in {"localhost", "127.0.0.1"}
        or url.database == "soborbum_reservations_check_20261029"
    )
    schema = "reservation_test_" + uuid4().hex
    root = create_engine(url)
    with root.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-c search_path={schema} -c lock_timeout=10000"}
    )
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            user = User(
                email="reserves@example.test",
                full_name="Проверка резерва",
                hashed_password="not-a-login",
                role=UserRole.ADMIN,
            )
            m = WarehouseMaterial(
                warehouse=Warehouse.TECHNOLOGY,
                category=MaterialCategory.NONE,
                title="Материал",
                code="PG-RESERVE",
                unit="шт",
                quantity_in_stock=10,
                purchase_price=10,
            )
            db.add_all([user, m])
            db.commit()
            uid, mid = user.id, m.id
        gate = threading.Barrier(2)

        def act(kind):
            with Session(engine) as db:
                actor = db.get(User, uid)
                gate.wait(timeout=10)
                try:
                    if kind == "reserve":
                        result = create(
                            Create(
                                request_id=uuid4(),
                                material_id=mid,
                                quantity=7,
                                purpose="Конкурирующий резерв",
                            ),
                            db,
                            actor,
                        )
                        return ("reserved", result["id"])
                    create_stock_operation(
                        db,
                        WarehouseOperationKind.ISSUE_MANUAL,
                        [(mid, 7)],
                        actor,
                        destination_kind=IssueDestinationKind.OBJECT,
                        destination="Объект",
                        received_by="Цех",
                    )
                    db.commit()
                    return ("issued", None)
                except HTTPException as e:
                    db.rollback()
                    return (e.status_code, None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(timeout=30)
                for f in [pool.submit(act, "reserve") for _ in range(2)]
            ]
        assert sorted(str(r[0]) for r in results) == ["409", "reserved"]
        rid = next(r[1] for r in results if r[0] == "reserved")
        with Session(engine) as db:
            assert db.get(WarehouseMaterial, mid).quantity_in_stock == 10
            release(
                rid,
                Change(
                    request_id=uuid4(), version=1, quantity=7, note="Повторная проверка"
                ),
                db,
                db.get(User, uid),
            )
        gate = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(timeout=30)
                for f in [pool.submit(act, kind) for kind in ("reserve", "issue")]
            ]
        assert sum(isinstance(r[0], str) for r in results) == 1
        with Session(engine) as db:
            m = db.get(WarehouseMaterial, mid)
            assert available(db, m) == 3
            assert m.quantity_in_stock in (3, 10)
    finally:
        engine.dispose()
        with root.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()
