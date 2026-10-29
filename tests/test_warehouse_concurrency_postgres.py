import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.cycle.models import Cycle, CycleStatus
from app.db import import_all_models  # noqa: F401
from app.db.base import Base
from app.production.models import BlockMaterial, Production, ProductionBlock
from app.production.service import request_material
from app.users.models import User, UserRole
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial
from app.warehouse.service import issue_by_techcard


@pytest.mark.skipif(not os.getenv('LOCK_TEST_DATABASE_URL'), reason='requires a dedicated PostgreSQL test service')
def test_simultaneous_issues_cannot_exceed_the_same_norm():
    url = make_url(os.environ['LOCK_TEST_DATABASE_URL'])
    assert url.get_backend_name() == 'postgresql' and url.host in {'localhost', '127.0.0.1'}
    schema = 'issue_test_' + uuid.uuid4().hex
    root = create_engine(url)
    with root.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={'options': f'-c search_path={schema} -c lock_timeout=10000'})
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            user = User(email='warehouse@example.test', full_name='Кладовщик', hashed_password='test', role=UserRole.WORKER)
            material = WarehouseMaterial(warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title='Брус', code='beam', unit='шт', quantity_in_stock=100)
            cycle = Cycle(status=CycleStatus.PRODUCTION)
            db.add_all([user, material, cycle]); db.flush()
            production = Production(cycle_id=cycle.id, house_index=1, name='Дом')
            db.add(production); db.flush()
            block = ProductionBlock(production_id=production.id, name='Каркас', sequence=1)
            db.add(block); db.flush()
            norm = BlockMaterial(block_id=block.id, warehouse_material_id=material.id, inventory_number='', unit='шт', quantity_required=5, quantity_requested=0, quantity_provided=0)
            db.add(norm); db.commit()
            user_id, material_id, production_id, norm_id = user.id, material.id, production.id, norm.id
        gate = threading.Barrier(2)
        def issue():
            with Session(engine) as db:
                actor = db.get(User, user_id); house = db.get(Production, production_id)
                gate.wait(timeout=10)
                try:
                    issue_by_techcard(db, house, actor, received_by='Цех', lines=[(material_id, 4)])
                    db.commit()
                    return 'issued'
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(issue) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        assert sorted(map(str, results)) == ['400', 'issued']
        with Session(engine) as db:
            norm = db.get(BlockMaterial, norm_id); material = db.get(WarehouseMaterial, material_id)
            assert (float(norm.quantity_required), float(norm.quantity_provided), float(material.quantity_in_stock)) == (1, 4, 96)
            # A new norm for the same material: production requests and direct
            # warehouse issues must coordinate on the same row as well.
            cycle = Cycle(status=CycleStatus.PRODUCTION); db.add(cycle); db.flush()
            house = Production(cycle_id=cycle.id, house_index=1, name='Второй дом'); db.add(house); db.flush()
            block = ProductionBlock(production_id=house.id, name='Стены', sequence=1); db.add(block); db.flush()
            norm2 = BlockMaterial(block_id=block.id, warehouse_material_id=material_id, inventory_number='', unit='шт', quantity_required=5, quantity_requested=0, quantity_provided=0)
            db.add(norm2); db.commit(); house2_id, norm2_id = house.id, norm2.id
        gate2 = threading.Barrier(2)
        def request_or_issue(action):
            with Session(engine) as db:
                actor = db.get(User, user_id); norm2 = db.get(BlockMaterial, norm2_id); house2 = db.get(Production, house2_id)
                gate2.wait(timeout=10)
                try:
                    if action == 'request':
                        request_material(db, norm2, 4, actor)
                    else:
                        issue_by_techcard(db, house2, actor, received_by='Цех', lines=[(material_id, 4)])
                    db.commit()
                    return 'ok'
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request_or_issue, action) for action in ('request', 'issue')]
            results = [future.result(timeout=30) for future in futures]
        assert sorted(map(str, results)) == ['400', 'ok']
        with Session(engine) as db:
            norm2 = db.get(BlockMaterial, norm2_id)
            assert float(norm2.quantity_required) == 1
            assert float(norm2.quantity_requested) + float(norm2.quantity_provided) == 4
    finally:
        engine.dispose()
        with root.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()
