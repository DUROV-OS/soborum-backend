"""Real row locks: competing transfers and counts must serialize on shared stock."""

import os
import threading
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
    StockDocument,
    StockMovement,
)
from app.warehouse.documents import Create, Post, create, post


@pytest.mark.skipif(
    not os.getenv("LOCK_TEST_DATABASE_URL"),
    reason="requires an isolated PostgreSQL test service",
)
def test_competing_transfers_and_inventory_cannot_replay_or_overwrite_stock():
    url = make_url(os.environ["LOCK_TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql"
    assert (
        url.host in {"localhost", "127.0.0.1"}
        or url.database == "soborbum_stock_check_20261029"
    )
    schema = "stock_document_test_" + uuid4().hex
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
                email="stock@example.test",
                full_name="Проверка склада",
                hashed_password="not-a-login",
                role=UserRole.ADMIN,
            )
            a = WarehouseMaterial(
                warehouse=Warehouse.TECHNOLOGY,
                category=MaterialCategory.NONE,
                title="Брус",
                code="PG-STOCK-DOC",
                unit="шт",
                quantity_in_stock=10,
                purchase_price=20,
            )
            db.add_all([actor, a])
            db.commit()
            actor_id, mid = actor.id, a.id
            bodies = [
                Create(
                    request_id=uuid4(),
                    kind="transfer",
                    warehouse=Warehouse.TECHNOLOGY,
                    target_warehouse=Warehouse.ID_GROUP,
                    note="Проверка параллельной выдачи",
                    lines=[{"material_id": mid, "quantity": 7}],
                )
                for _ in range(2)
            ]
            docs = [create(body, db, actor) for body in bodies]
        gate = threading.Barrier(2)

        def conduct(doc, key=None):
            with Session(engine) as db:
                actor = db.get(User, actor_id)
                gate.wait(timeout=10)
                try:
                    result = post(
                        doc["id"],
                        Post(version=doc["version"], request_id=key or uuid4()),
                        db,
                        actor,
                    )
                    return result["status"]
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(conduct, doc) for doc in docs]
            results = [f.result(timeout=30) for f in futures]
        assert sorted(map(str, results)) == ["409", "posted"]
        with Session(engine) as db:
            assert db.get(WarehouseMaterial, mid).quantity_in_stock == 3
            assert (
                db.query(WarehouseMaterial)
                .filter_by(warehouse=Warehouse.ID_GROUP)
                .one()
                .quantity_in_stock
                == 7
            )
            assert db.query(StockMovement).count() == 2
            actor = db.get(User, actor_id)
            count = create(
                Create(
                    request_id=uuid4(),
                    kind="inventory",
                    warehouse=Warehouse.TECHNOLOGY,
                    note="Повторный пересчёт",
                    lines=[{"material_id": mid, "quantity": 2}],
                ),
                db,
                actor,
            )
        # Two identical request IDs on one document produce just one count loss.
        gate = threading.Barrier(2)
        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(conduct, count, key) for _ in range(2)]
            assert [f.result(timeout=30) for f in futures] == ["posted", "posted"]
        with Session(engine) as db:
            assert db.get(WarehouseMaterial, mid).quantity_in_stock == 2
            assert db.query(StockMovement).count() == 3
            assert db.query(StockDocument).filter_by(status="posted").count() == 2
    finally:
        engine.dispose()
        with root.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()
