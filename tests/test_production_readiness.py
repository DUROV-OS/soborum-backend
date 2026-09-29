"""Оценка готовности производства и блока (0084-b): отсутствие данных —
отдельное состояние, а не молчаливый «зелёный»; допуск блока отделён от
материалов.
"""

from datetime import datetime, timedelta, timezone

from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.production import readiness, stage_plan
from app.production import service as production_service
from app.production.models import BlockMaterial, Production, ProductionBlock
from app.production.readiness import MaterialsState
from app.production.stage_templates import ProductionStageTemplate, TemplateBlock, TemplateBlockMaterial, TemplateStatus
from app.tasks import service as task_service
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.warehouse import service as warehouse_service
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial


def _make_production(db):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    return production


def _make_block(db, production, name, sequence=1, requires_materials=True):
    block = ProductionBlock(
        production_id=production.id, name=name, sequence=sequence, requires_materials=requires_materials
    )
    db.add(block)
    db.flush()
    return block


def _make_warehouse_material(db, title="Свая винтовая", stock=100):
    material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title=title,
        code=f"M-{title}", unit="шт", quantity_in_stock=stock,
    )
    db.add(material)
    db.flush()
    return material


def _add_material(db, block, warehouse_material, required=0.0):
    material = BlockMaterial(
        block_id=block.id, warehouse_material_id=warehouse_material.id, inventory_number="",
        unit="шт", quantity_required=required, quantity_requested=0, quantity_provided=0,
    )
    db.add(material)
    db.flush()
    return material


def _block_state(db, block):
    db.expire_all()
    return readiness.assess_block(db, db.get(ProductionBlock, block.id))


def test_block_without_materials_is_insufficient_data(db):
    production = _make_production(db)
    block = _make_block(db, production, "Стены")
    db.commit()

    assessment = _block_state(db, block)
    assert assessment.materials_state == MaterialsState.INSUFFICIENT_DATA
    assert [r.code for r in assessment.reasons] == ["no_materials"]
    assert assessment.version == "readiness-v1"


def test_zero_quantity_is_insufficient_data_even_next_to_a_real_shortfall(db):
    production = _make_production(db)
    block = _make_block(db, production, "Стены")
    wm = _make_warehouse_material(db)
    _add_material(db, block, wm, required=0)
    _add_material(db, block, _make_warehouse_material(db, title="Брус"), required=0)
    _add_material(db, block, _make_warehouse_material(db, title="Утеплитель"), required=5)
    db.commit()

    assessment = _block_state(db, block)
    assert assessment.materials_state == MaterialsState.INSUFFICIENT_DATA
    unset = next(r for r in assessment.reasons if r.code == "quantity_not_set")
    assert unset.text == "Блок «Стены»: у 2 материалов не указано количество"
    # Нехватка по третьему материалу тоже видна в причинах, хоть состояние и хуже.
    assert any(r.code == "not_requested" for r in assessment.reasons)


def test_open_material_match_task_needs_reconciliation(db):
    production = _make_production(db)
    block = _make_block(db, production, "Каркас")
    _add_material(db, block, _make_warehouse_material(db), required=3)
    match = task_service.create_task(
        db, title="Сопоставить со складом материал «Брус» (м3) и добавить в блок «Каркас»",
        block_id=block.id, link_type=TaskLinkType.BLOCK_MATERIAL_MATCH, link_id=block.id,
    )
    db.commit()

    assessment = _block_state(db, block)
    assert assessment.materials_state == MaterialsState.NEEDS_RECONCILIATION
    reason = next(r for r in assessment.reasons if r.code == "material_match_open")
    assert reason.task_id == match.id

    task_service.force_close(db, match)
    db.commit()
    assert _block_state(db, block).materials_state == MaterialsState.SHORTFALL


def test_shortfall_reports_not_requested_and_awaiting_issue(db, make_user):
    user = make_user(Module.PRODUCTION)
    production = _make_production(db)
    block = _make_block(db, production, "Фундамент")
    material = _add_material(db, block, _make_warehouse_material(db), required=10)
    db.commit()

    assessment = _block_state(db, block)
    assert assessment.materials_state == MaterialsState.SHORTFALL
    assert [r.text for r in assessment.reasons] == ["Блок «Фундамент»: «Свая винтовая» — не заказано 10 шт"]

    production_service.request_material(db, material, 4, user)
    db.commit()
    assessment = _block_state(db, block)
    assert assessment.materials_state == MaterialsState.SHORTFALL
    assert sorted(r.code for r in assessment.reasons) == ["awaiting_issue", "not_requested"]
    assert any("ждёт выдачи со склада 4 шт" in r.text for r in assessment.reasons)


def test_everything_issued_by_requests_is_provided(db, make_user):
    engineer = make_user(Module.PRODUCTION)
    storekeeper = make_user(Module.WAREHOUSE)
    production = _make_production(db)
    block = _make_block(db, production, "Фундамент")
    material = _add_material(db, block, _make_warehouse_material(db), required=10)
    db.commit()

    request = production_service.request_material(db, material, 10, engineer)
    db.commit()
    warehouse_service.approve_request(db, request, storekeeper)
    db.commit()

    assessment = _block_state(db, block)
    assert assessment.materials_state == MaterialsState.PROVIDED
    # «Обеспечены» — только «выдано по заявкам», без сверки с остатком (P0).
    assert assessment.reasons[0].code == "provided_by_requests"
    assert "складской остаток не сверялся" in assessment.reasons[0].text
    assert request.id in assessment.sources.material_request_ids


def test_block_marked_not_required(db):
    production = _make_production(db)
    block = _make_block(db, production, "Документация", requires_materials=False)
    db.commit()

    assessment = _block_state(db, block)
    assert assessment.materials_state == MaterialsState.NOT_REQUIRED
    assert assessment.admitted is True


def test_admitted_depends_only_on_closed_dependencies(db):
    production = _make_production(db)
    foundation = _make_block(db, production, "Фундамент", sequence=1)
    frame = _make_block(db, production, "Каркас", sequence=2, requires_materials=False)
    empty = _make_block(db, production, "Проект", sequence=0)
    frame.depends_on = [foundation]
    task = task_service.create_task(db, title="Завинтить сваи", block_id=foundation.id)
    db.commit()

    assessment = _block_state(db, frame)
    assert assessment.admitted is False
    assert [(w.block_id, w.name) for w in assessment.waiting_on] == [(foundation.id, "Фундамент")]
    # Допуск не смешивается с материалами.
    assert assessment.materials_state == MaterialsState.NOT_REQUIRED
    assert _block_state(db, foundation).admitted is True

    task_service.force_close(db, db.get(Task, task.id))
    db.commit()
    assert _block_state(db, frame).admitted is True

    # Зависимость без задач закрытой не считается — закрывать в ней нечего.
    frame = db.get(ProductionBlock, frame.id)
    frame.depends_on = [db.get(ProductionBlock, foundation.id), db.get(ProductionBlock, empty.id)]
    db.commit()
    assessment = _block_state(db, frame)
    assert assessment.admitted is False
    assert [w.block_id for w in assessment.waiting_on] == [empty.id]


def test_production_takes_worst_block_state(db):
    production = _make_production(db)
    provided_less = _make_block(db, production, "Документация", sequence=1, requires_materials=False)
    shortfall = _make_block(db, production, "Фундамент", sequence=2)
    _add_material(db, shortfall, _make_warehouse_material(db), required=2)
    db.commit()

    assessment = readiness.assess_production(db, production)
    assert assessment.materials_state == MaterialsState.SHORTFALL
    assert [b.block_id for b in assessment.blocks] == [provided_less.id, shortfall.id]
    # Причины — от худшего блока к лучшему.
    assert assessment.reasons[0].block_id == shortfall.id
    assert [r.code for r in assessment.problem_reasons] == ["not_requested"]

    empty = _make_block(db, production, "Стены", sequence=3)
    db.commit()
    db.expire_all()
    assessment = readiness.assess_production(db, db.get(Production, production.id))
    assert assessment.materials_state == MaterialsState.INSUFFICIENT_DATA
    assert assessment.reasons[0].block_id == empty.id


def test_production_without_blocks_is_insufficient_data(db):
    production = _make_production(db)
    db.commit()

    assessment = readiness.assess_production(db, production)
    assert assessment.materials_state == MaterialsState.INSUFFICIENT_DATA
    assert [r.code for r in assessment.reasons] == ["no_blocks"]
    assert assessment.facts_at is None


def test_facts_at_moves_when_quantity_changes(db, make_user):
    user = make_user(Module.PRODUCTION)
    production = _make_production(db)
    block = _make_block(db, production, "Фундамент")
    material = _add_material(db, block, _make_warehouse_material(db), required=0)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.query(BlockMaterial).filter(BlockMaterial.id == material.id).update({"updated_at": old})
    db.query(ProductionBlock).filter(ProductionBlock.id == block.id).update({"updated_at": old})
    db.commit()
    assert _block_state(db, block).facts_at == old

    production_service.update_required_quantity(db, db.get(BlockMaterial, material.id), 5, user)
    db.commit()
    assessment = _block_state(db, block)
    assert assessment.materials_state == MaterialsState.SHORTFALL
    assert assessment.facts_at > old + timedelta(days=1)


def _make_template(db, admin_id):
    template = ProductionStageTemplate(
        house_model_key=None, status=TemplateStatus.CONFIRMED, source_client_id=1, confirmed_by_id=admin_id,
    )
    db.add(template)
    db.flush()
    wm = _make_warehouse_material(db)
    foundation = TemplateBlock(template_id=template.id, name="Фундамент", sequence=1, kr_page_refs=[])
    frame = TemplateBlock(template_id=template.id, name="Каркас", sequence=2, kr_page_refs=[])
    docs = TemplateBlock(
        template_id=template.id, name="Документация", sequence=3, kr_page_refs=[], requires_materials=False
    )
    db.add_all([foundation, frame, docs])
    db.flush()
    db.add(TemplateBlockMaterial(template_block_id=foundation.id, name="Свая", unit="шт", warehouse_material_id=wm.id))
    db.add(TemplateBlockMaterial(template_block_id=frame.id, name="Брус 150х150", unit="м3", warehouse_material_id=None))
    db.flush()
    return template


def test_freshly_generated_plan_is_not_green(db, make_user):
    admin = make_user(admin=True)
    production = _make_production(db)
    template = _make_template(db, admin.id)
    db.commit()

    stage_plan.instantiate_stage_plan(db, production, template)
    db.commit()

    assessment = readiness.assess_production(db, production)
    by_name = {b.name: b for b in assessment.blocks}
    assert assessment.materials_state == MaterialsState.INSUFFICIENT_DATA
    # Материал плана с количеством 0 — не «нехватки нет», а «не указано».
    assert [r.code for r in by_name["Фундамент"].reasons] == ["quantity_not_set"]
    # Несопоставленный материал: строки нет, открыта задача сверки со своим link_type.
    assert by_name["Каркас"].materials_state == MaterialsState.INSUFFICIENT_DATA
    assert [r.code for r in by_name["Каркас"].reasons] == ["no_materials", "material_match_open"]
    match_task = db.query(Task).filter(Task.block_id == by_name["Каркас"].block_id).one()
    assert match_task.link_type == TaskLinkType.BLOCK_MATERIAL_MATCH
    # requires_materials копируется из шаблона.
    assert by_name["Документация"].materials_state == MaterialsState.NOT_REQUIRED


# ------------------------------------------------------------------ API --


def test_readiness_endpoints(api, make_user, db):
    production = _make_production(db)
    block = _make_block(db, production, "Стены")
    other = _make_production(db)
    _make_block(db, other, "Документация", requires_materials=False)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    detail = worker.get(f"/api/production/{production.id}/readiness")
    assert detail.status_code == 200
    body = detail.json()
    assert body["materials_state"] == "insufficient_data"
    assert body["materials_label"] == "Недостаточно данных"
    assert body["version"] == "readiness-v1"
    assert body["blocks"][0]["block_id"] == block.id
    assert body["blocks"][0]["admitted"] is True
    assert body["reasons"][0] == {
        "code": "no_materials", "text": "Блок «Стены»: материалы не указаны",
        "block_id": block.id, "material_id": None, "task_id": None,
    }
    assert body["sources"]["block_ids"] == [block.id]

    listing = worker.get("/api/production/readiness")
    assert listing.status_code == 200
    items = {item["production_id"]: item for item in listing.json()}
    assert items[production.id]["materials_state"] == "insufficient_data"
    assert items[production.id]["reasons_count"] == 1
    assert items[other.id]["materials_state"] == "not_required"
    # Пояснение «помечен: не требуются» — не причина для внимания.
    assert items[other.id]["reasons_count"] == 0

    assert worker.get("/api/production/999999/readiness").status_code == 404


def test_readiness_endpoints_need_production_access(api, make_user, db):
    production = _make_production(db)
    db.commit()
    outsider = api(make_user(Module.WAREHOUSE))

    assert outsider.get(f"/api/production/{production.id}/readiness").status_code == 403
    assert outsider.get("/api/production/readiness").status_code == 403


def test_block_requires_materials_is_editable_via_block_patch(api, make_user, db):
    production = _make_production(db)
    block = _make_block(db, production, "Документация")
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.patch(f"/api/production/blocks/{block.id}", json={"requires_materials": False})
    assert resp.status_code == 200
    assert resp.json()["requires_materials"] is False

    readiness_resp = worker.get(f"/api/production/{production.id}/readiness")
    assert readiness_resp.json()["materials_state"] == "not_required"

    # null не ломает NOT NULL колонку — значит «не менять».
    resp = worker.patch(f"/api/production/blocks/{block.id}", json={"requires_materials": None})
    assert resp.status_code == 200
    assert resp.json()["requires_materials"] is False
