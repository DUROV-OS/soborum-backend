"""Направленный граф этапов производства (0066-a): порядок (`sequence`) и
зависимости между блоками одного производства, с защитой от циклов.
"""

from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.production.models import Production, ProductionBlock


def _make_production(db):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    return production


def _make_block(db, production, name, sequence=None):
    block = ProductionBlock(production_id=production.id, name=name, sequence=sequence or 0)
    db.add(block)
    db.flush()
    return block


def test_create_block_assigns_incrementing_sequence(api, make_user, db):
    production = _make_production(db)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    first = worker.post(f"/api/production/{production.id}/blocks", json={"name": "Фундамент"})
    second = worker.post(f"/api/production/{production.id}/blocks", json={"name": "Каркас"})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["sequence"] == 1
    assert second.json()["sequence"] == 2


def test_add_block_dependency(api, make_user, db):
    production = _make_production(db)
    foundation = _make_block(db, production, "Фундамент", sequence=1)
    frame = _make_block(db, production, "Каркас", sequence=2)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.post(
        f"/api/production/blocks/{frame.id}/dependencies", json={"depends_on_id": foundation.id}
    )
    assert resp.status_code == 201
    assert resp.json()["depends_on_ids"] == [foundation.id]


def test_dependency_cannot_close_a_cycle(api, make_user, db):
    production = _make_production(db)
    foundation = _make_block(db, production, "Фундамент", sequence=1)
    frame = _make_block(db, production, "Каркас", sequence=2)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    ok = worker.post(f"/api/production/blocks/{frame.id}/dependencies", json={"depends_on_id": foundation.id})
    assert ok.status_code == 201

    cycle = worker.post(
        f"/api/production/blocks/{foundation.id}/dependencies", json={"depends_on_id": frame.id}
    )
    assert cycle.status_code == 400


def test_dependency_rejects_self_reference(api, make_user, db):
    production = _make_production(db)
    block = _make_block(db, production, "Фундамент", sequence=1)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.post(f"/api/production/blocks/{block.id}/dependencies", json={"depends_on_id": block.id})
    assert resp.status_code == 400


def test_dependency_rejects_block_from_another_production(api, make_user, db):
    production = _make_production(db)
    other_production = _make_production(db)
    block = _make_block(db, production, "Фундамент", sequence=1)
    other_block = _make_block(db, other_production, "Другой дом", sequence=1)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.post(
        f"/api/production/blocks/{block.id}/dependencies", json={"depends_on_id": other_block.id}
    )
    assert resp.status_code == 400


def test_remove_block_dependency(api, make_user, db):
    production = _make_production(db)
    foundation = _make_block(db, production, "Фундамент", sequence=1)
    frame = _make_block(db, production, "Каркас", sequence=2)
    frame.depends_on = [foundation]
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.delete(f"/api/production/blocks/{frame.id}/dependencies/{foundation.id}")
    assert resp.status_code == 200
    assert resp.json()["depends_on_ids"] == []
