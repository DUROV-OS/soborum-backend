"""Виджет «Сроки» одного производства (0065-b): главное узкое место и его
влияние на срок, с детерминированным fallback без ИИ и «по графику», когда
сигналов действительно нет — а не выдуманная причина."""

from datetime import datetime, timedelta, timezone

from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.production import deadlines
from app.production.models import BlockMaterial, MaterialRequest, MaterialRequestStatus, Production, ProductionBlock
from app.tasks.models import Task, TaskStatus
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial


def _make_production(db):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    return production


def _make_block(db, production, name="Каркас", requires_materials=True):
    block = ProductionBlock(production_id=production.id, name=name, requires_materials=requires_materials)
    db.add(block)
    db.flush()
    return block


def _make_warehouse_material(db, title="Брус"):
    material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title=title,
        code=f"M-{db.query(WarehouseMaterial).count()}", unit="шт", quantity_in_stock=100,
    )
    db.add(material)
    db.flush()
    return material


def test_no_signals_means_on_schedule(db):
    production = _make_production(db)
    _make_block(db, production, name="Документация", requires_materials=False)
    db.commit()
    result = deadlines.generate_deadline_insight(db, production)
    assert result.source == "none"
    assert result.title == "По графику"
    assert result.impact == ""
    assert result.generated_at is not None


def test_insufficient_data_is_not_on_schedule(db):
    """0084-c: блок без количеств — «прогноз не построен», а не «по графику»."""
    production = _make_production(db)
    block = _make_block(db, production, name="Стены")
    db.add(BlockMaterial(
        block_id=block.id, warehouse_material_id=_make_warehouse_material(db).id,
        inventory_number="", unit="шт", quantity_required=0,
    ))
    db.commit()

    result = deadlines.generate_deadline_insight(db, production)
    assert result.title == "Прогноз не построен"
    assert result.source == "insufficient_data"
    assert "Блок «Стены»: у 1 материала не указано количество" in result.description

    empty = _make_production(db)
    db.commit()
    assert deadlines.generate_deadline_insight(db, empty).title == "Прогноз не построен"


def test_overdue_task_fallback_names_the_block_and_task(db, make_user):
    production = _make_production(db)
    block = _make_block(db, production, name="Кровля", requires_materials=False)
    assignee = make_user(Module.PRODUCTION)
    task = Task(
        title="Смонтировать стропила", block_id=block.id, status=TaskStatus.READY,
        deadline=datetime.now(timezone.utc) - timedelta(days=3),
    )
    task.assignees = [assignee]
    db.add(task)
    db.commit()

    result = deadlines.generate_deadline_insight(db, production)
    assert result.source == "fallback"
    assert "Кровля" in result.title
    assert "Смонтировать стропила" in result.description
    assert result.impact


def test_pending_material_request_outranks_unrequested_shortfall(db, make_user):
    production = _make_production(db)
    block = _make_block(db, production)
    requester = make_user(Module.PRODUCTION)
    warehouse_material = _make_warehouse_material(db)

    requested = BlockMaterial(
        block_id=block.id, warehouse_material_id=warehouse_material.id,
        inventory_number="INV-1", unit="шт", quantity_required=5, quantity_requested=5,
    )
    unrequested = BlockMaterial(
        block_id=block.id, warehouse_material_id=warehouse_material.id,
        inventory_number="INV-2", unit="шт", quantity_required=3,
    )
    db.add_all([requested, unrequested])
    db.flush()
    db.add(MaterialRequest(
        block_material_id=requested.id, warehouse_material_id=warehouse_material.id,
        quantity=5, status=MaterialRequestStatus.PENDING, requested_by_id=requester.id,
    ))
    db.commit()

    result = deadlines.generate_deadline_insight(db, production)
    assert result.source == "fallback"
    assert "склада" in result.title.lower()


def test_ai_result_is_cached_until_ttl(db, monkeypatch):
    production = _make_production(db)
    block = _make_block(db, production, requires_materials=False)
    task = Task(
        title="Задача", block_id=block.id, status=TaskStatus.READY,
        deadline=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(task)
    db.commit()

    calls = {"n": 0}

    def fake_ai_pick(signals):
        calls["n"] += 1
        return {"title": "Т", "description": "Д", "impact": "И"}

    monkeypatch.setattr(deadlines, "_ai_pick_bottleneck", fake_ai_pick)

    first = deadlines.generate_deadline_insight(db, production)
    second = deadlines.generate_deadline_insight(db, production)

    assert calls["n"] == 1
    assert first.source == "ai" and second.source == "ai"
    assert second.title == "Т"


def test_force_recomputes_bypassing_cache(db, monkeypatch):
    production = _make_production(db)
    block = _make_block(db, production, requires_materials=False)
    task = Task(
        title="Задача", block_id=block.id, status=TaskStatus.READY,
        deadline=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(task)
    db.commit()

    calls = {"n": 0}

    def fake_ai_pick(signals):
        calls["n"] += 1
        return {"title": "Т", "description": "Д", "impact": "И"}

    monkeypatch.setattr(deadlines, "_ai_pick_bottleneck", fake_ai_pick)

    deadlines.generate_deadline_insight(db, production)
    deadlines.generate_deadline_insight(db, production, force=True)

    assert calls["n"] == 2


def test_home_endpoint_returns_deadlines_without_anthropic_key(api, make_user, db):
    production = _make_production(db)
    _make_block(db, production, requires_materials=False)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))
    resp = worker.get(f"/api/production/{production.id}/home")
    assert resp.status_code == 200
    body = resp.json()["deadlines"]
    assert body["source"] == "none"
    assert body["title"] == "По графику"
