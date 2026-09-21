"""Автосопоставление материала шаблона графа этапов со складом (0073-a):

- high/medium сохраняются в TemplateMaterialMapping и сразу используются;
- no_match не кэшируется и не мешает прежнему ручному фолбэку в
  stage_plan.py (задача «сопоставить со складом вручную»);
- повторное сопоставление того же материала (name+unit) переиспользует
  справочник без повторного обращения к ИИ («вторая генерация»);
- ИИ-генерация шаблона (_persist_draft) сама заполняет warehouse_material_id
  и confidence по мере сборки материалов.

Единственная сетевая точка (_call_ai) монки-патчится, как и в
test_production_stage_templates.py — реальный Anthropic-клиент здесь не
вызывается.
"""

from app.clients.models import Client
from app.core.config import settings
from app.cycle.models import Cycle, CycleStatus
from app.common.module_access import Module
from app.production import material_matching, stage_template_service
from app.production.models import (
    KrExtraction,
    MappingConfidence,
    MatchedBy,
    Production,
    ProductionBlock,
    TemplateMaterialMapping,
)
from app.production import stage_plan
from app.production.stage_templates import (
    ProductionStageTemplate,
    TemplateBlock,
    TemplateBlockMaterial,
    TemplateStatus,
)
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial


def _make_warehouse_material(db, title="Свая винтовая 108мм", code="M-1", unit="шт"):
    material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title=title, code=code, unit=unit,
        quantity_in_stock=10,
    )
    db.add(material)
    db.flush()
    return material


def test_high_confidence_match_is_saved_and_reused_immediately(db, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    material = _make_warehouse_material(db)
    db.commit()

    calls = {"n": 0}

    def fake_call_ai(name, unit, candidates):
        calls["n"] += 1
        return {"warehouse_material_id": material.id, "confidence": "high"}

    monkeypatch.setattr(material_matching, "_call_ai", fake_call_ai)

    result = material_matching.match_template_material(db, "Свая винтовая 108мм", "шт")
    db.commit()

    assert calls["n"] == 1
    assert result.warehouse_material_id == material.id
    assert result.confidence == "high"

    mapping = db.query(TemplateMaterialMapping).one()
    assert mapping.warehouse_material_id == material.id
    assert mapping.matched_by == MatchedBy.AI
    assert mapping.confidence.value == "high"
    assert mapping.normalized_name == "свая винтовая 108мм"


def test_medium_confidence_match_is_saved_as_needing_review(db, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    material = _make_warehouse_material(db, title="Брус 150х150", code="M-2", unit="м3")
    db.commit()

    monkeypatch.setattr(
        material_matching, "_call_ai",
        lambda name, unit, candidates: {"warehouse_material_id": material.id, "confidence": "medium"},
    )

    result = material_matching.match_template_material(db, "Брус 150х150", "м3")
    db.commit()

    assert result.warehouse_material_id == material.id
    assert result.confidence == "medium"

    mapping = db.query(TemplateMaterialMapping).one()
    assert mapping.confidence.value == "medium"
    assert mapping.matched_by == MatchedBy.AI


def test_no_match_is_not_cached_and_manual_fallback_task_still_created(db, monkeypatch, make_user):
    """ИИ явно отказывается — как и предписано (не выдумывать совпадение):
    ничего не попадает в TemplateMaterialMapping, warehouse_material_id
    остаётся NULL, а инстанциация плана (stage_plan.py, эта задача её не
    трогает) заводит прежнюю задачу «сопоставить со складом вручную»."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    _make_warehouse_material(db)  # каталог не пуст, но ничего подходящего
    db.commit()

    calls = {"n": 0}

    def fake_call_ai(name, unit, candidates):
        calls["n"] += 1
        return {"no_match": True}

    monkeypatch.setattr(material_matching, "_call_ai", fake_call_ai)

    result = material_matching.match_template_material(db, "Неизвестный материал", "шт")
    db.commit()

    assert calls["n"] == 1
    assert result.warehouse_material_id is None
    assert result.confidence is None
    assert db.query(TemplateMaterialMapping).count() == 0

    admin = make_user(admin=True)
    make_user(Module.PRODUCTION)
    template = ProductionStageTemplate(status=TemplateStatus.CONFIRMED, source_client_id=1, confirmed_by_id=admin.id)
    db.add(template)
    db.flush()
    block = TemplateBlock(template_id=template.id, name="Фундамент", sequence=1, kr_page_refs=[])
    db.add(block)
    db.flush()
    db.add(TemplateBlockMaterial(
        template_block_id=block.id, name="Неизвестный материал", unit="шт", warehouse_material_id=None,
    ))
    db.flush()

    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.commit()

    stage_plan.instantiate_stage_plan(db, production, template)
    db.commit()

    real_block = db.query(ProductionBlock).filter(ProductionBlock.production_id == production.id).one()
    assert real_block.materials == []
    assert any("Сопоставить со складом" in t.title for t in real_block.tasks)


def test_second_generation_reuses_mapping_without_calling_ai_again(db, monkeypatch):
    """Переиспользование справочника между «генерациями»: одна и та же пара
    имя/ед. материала — второй вызов match_template_material не должен
    обращаться к ИИ повторно."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    material = _make_warehouse_material(db, title="Брус 150х150", code="M-3", unit="м3")
    db.commit()

    calls = {"n": 0}

    def fake_call_ai(name, unit, candidates):
        calls["n"] += 1
        return {"warehouse_material_id": material.id, "confidence": "high"}

    monkeypatch.setattr(material_matching, "_call_ai", fake_call_ai)

    first = material_matching.match_template_material(db, "Брус 150х150", "м3")
    db.commit()
    second = material_matching.match_template_material(db, "Брус 150х150", "м3")
    db.commit()

    assert calls["n"] == 1, "второй вызов должен был взять сопоставление из TemplateMaterialMapping, не звать ИИ снова"
    assert first.warehouse_material_id == second.warehouse_material_id == material.id
    assert first.confidence == second.confidence == "high"
    assert db.query(TemplateMaterialMapping).count() == 1


def test_generation_hook_persists_ai_match_on_new_template(db, make_user, monkeypatch):
    """_persist_draft (stage_template_service) вызывает сопоставление сразу
    в проходе сборки TemplateBlockMaterial — сохранённый материал шаблона
    должен нести warehouse_material_id/confidence, полученные от ИИ."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    material = _make_warehouse_material(db, title="Свая винтовая 108мм", code="M-4", unit="шт")

    cycle = Cycle()
    db.add(cycle)
    db.flush()
    client = Client(cycle_id=cycle.id, full_name="Клиент 0073-a", phone="+79160000000", email="m@example.com")
    db.add(client)
    db.flush()
    db.add(KrExtraction(client_id=client.id, pages=[{"page_number": 1, "text": "текст", "image_file_id": 1}]))
    db.commit()

    graph = {
        "blocks": [
            {
                "name": "Фундамент",
                "sequence": 1,
                "depends_on_sequence": [],
                "kr_page_refs": [],
                "tasks": [],
                "materials": [{"name": "Свая винтовая 108мм", "unit": "шт", "kr_page_ref": None}],
            }
        ]
    }
    monkeypatch.setattr(stage_template_service, "_call_ai", lambda pages_payload: graph)
    monkeypatch.setattr(
        material_matching, "_call_ai",
        lambda name, unit, candidates: {"warehouse_material_id": material.id, "confidence": "high"},
    )

    template = stage_template_service.generate_or_reuse_template(db, client)
    db.commit()

    saved_material = template.blocks[0].materials[0]
    assert saved_material.warehouse_material_id == material.id
    assert saved_material.confidence.value == "high"


def test_human_correction_wins_over_ai_and_is_not_reasked(db, monkeypatch):
    """Ручная правка на проверке шаблона (update_material) пишется в
    TemplateMaterialMapping как matched_by=human и с этого момента побеждает
    над более ранним ИИ-сопоставлением той же пары имя/ед. — следующее
    сопоставление того же материала переиспользует правку человека, не
    переспрашивая ИИ."""
    material_name = "Свая монтажная 108"
    unit = "шт"
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    wrong = _make_warehouse_material(db, title="Свая винтовая 89мм", code="M-5", unit=unit)
    correct = _make_warehouse_material(db, title="Свая винтовая 108мм", code="M-6", unit=unit)
    db.commit()

    calls = {"n": 0}

    def fake_call_ai(name, u, candidates):
        calls["n"] += 1
        return {"warehouse_material_id": wrong.id, "confidence": "medium"}

    monkeypatch.setattr(material_matching, "_call_ai", fake_call_ai)

    # Первая "генерация" — ИИ предлагает неверную (но не бессмысленную) карточку.
    first = material_matching.match_template_material(db, material_name, unit)
    db.commit()
    assert first.warehouse_material_id == wrong.id
    assert calls["n"] == 1

    from app.production.schemas import TemplateBlockMaterialPatch

    template = ProductionStageTemplate(status=TemplateStatus.DRAFT, source_client_id=1)
    db.add(template)
    db.flush()
    block = TemplateBlock(template_id=template.id, name="Фундамент", sequence=1, kr_page_refs=[])
    db.add(block)
    db.flush()
    material = TemplateBlockMaterial(
        template_block_id=block.id, name=material_name, unit=unit,
        warehouse_material_id=wrong.id, confidence=MappingConfidence.MEDIUM,
    )
    db.add(material)
    db.commit()

    # Инженер на проверке шаблона поправляет на правильную карточку.
    stage_template_service.update_material(
        db, template, material, TemplateBlockMaterialPatch(warehouse_material_id=correct.id)
    )
    db.commit()

    assert material.warehouse_material_id == correct.id
    assert material.confidence == MappingConfidence.HIGH

    mapping = material_matching._lookup_cached(db, material_matching._normalize(material_name), unit)
    assert mapping is not None
    assert mapping.matched_by == MatchedBy.HUMAN
    assert mapping.warehouse_material_id == correct.id
    assert mapping.confidence == MappingConfidence.HIGH

    # Вторая "генерация" того же материала — переиспользует правку человека,
    # ИИ повторно не вызывается (calls не растёт с 1).
    second = material_matching.match_template_material(db, material_name, unit)
    db.commit()
    assert second.warehouse_material_id == correct.id
    assert second.confidence == "high"
    assert calls["n"] == 1, "человеческая правка не должна переспрашивать ИИ повторно"
