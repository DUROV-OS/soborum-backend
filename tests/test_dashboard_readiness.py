"""Потребители оценки готовности (0084-c): Пульс, «Работа», «Главная»
производства, Марина и ИИ-аналитика смотрят на одну оценку
(app/production/readiness.py); ИИ не может быть «зеленее» фактов; кэш
сбрасывается при изменении материалов.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.agents.connectors import _production_line
from app.ai import analytics
from app.ai import cache as ai_cache
from app.ai.models import AiCacheEntry
from app.common.module_access import Module
from app.core import llm
from app.core.config import settings
from app.cycle.models import Cycle, CycleStatus
from app.dashboard import overview
from app.dashboard.service import _snapshot_production
from app.production import service as production_service
from app.production.models import BlockMaterial, Production, ProductionBlock
from app.tasks import service as task_service
from app.tasks.models import TaskLinkType
from app.warehouse import service as warehouse_service
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial

PRODUCTION_KEYS = ["section_analytics:production", "dashboard_section_signal:production"]


def _make_production(db, cycle_status=CycleStatus.PRODUCTION, name="Дом"):
    cycle = Cycle(status=cycle_status)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name=name)
    db.add(production)
    db.flush()
    return production


def _make_block(db, production, name="Стены", requires_materials=True):
    block = ProductionBlock(production_id=production.id, name=name, sequence=1, requires_materials=requires_materials)
    db.add(block)
    db.flush()
    return block


def _add_material(db, block, required=0.0):
    wm = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title="Брус",
        code=f"M-{db.query(WarehouseMaterial).count()}", unit="шт", quantity_in_stock=100,
    )
    db.add(wm)
    db.flush()
    material = BlockMaterial(
        block_id=block.id, warehouse_material_id=wm.id, inventory_number="", unit="шт",
        quantity_required=required, quantity_requested=0, quantity_provided=0,
    )
    db.add(material)
    db.flush()
    return material


def _warm_caches(db, production_id):
    now = datetime.now(timezone.utc)
    for key in PRODUCTION_KEYS + [f"production_home_deadlines:{production_id}"]:
        ai_cache.set(db, key, {"stale": True}, now)


def _cached_keys(db):
    return {entry.key for entry in db.query(AiCacheEntry).all()}


# ------------------------------------------------------- снимок и Пульс --


def test_snapshot_counts_productions_by_readiness_state(db):
    unset = _make_production(db)
    _add_material(db, _make_block(db, unset), required=0)
    shortfall = _make_production(db, name="Баня")
    _add_material(db, _make_block(db, shortfall, name="Фундамент"), required=5)
    fine = _make_production(db)
    _make_block(db, fine, name="Документация", requires_materials=False)
    # Завершённый дом без материалов — не в работе, в оценку не входит.
    _make_production(db, cycle_status=CycleStatus.COMPLETED)
    db.commit()

    snap = _snapshot_production(db)
    assert "blocks_with_material_shortfall" not in snap
    assert snap["productions_in_work"] == 3
    assert snap["productions_insufficient_data"] == 1
    assert snap["productions_shortfall"] == 1
    assert snap["productions_not_required"] == 1
    assert snap["productions_needing_attention"] == 2
    assert snap["worst_state"] == "insufficient_data"
    assert snap["top_reasons"][0] == {
        "production_id": unset.id, "production": f"Заказ №{unset.cycle_id}",
        "code": "quantity_not_set", "text": "Блок «Стены»: у 1 материала не указано количество",
    }
    assert snap["top_reasons"][1]["production"] == f"Заказ №{shortfall.cycle_id} · Баня"


def test_today_widget_tone_and_attention_queue_follow_readiness(db, make_user):
    production = _make_production(db)
    _add_material(db, _make_block(db, production), required=0)
    db.commit()
    user = make_user(Module.PRODUCTION)

    today = overview.generate_today(db, user)
    widget = next(w for w in today.widgets if w.section == "production" and w.title.startswith("Производств с"))
    assert widget.tone == "warning"
    assert widget.value == "1"
    assert widget.hint == "Недостаточно данных"
    action = next(a for a in today.actions if a.id == f"production:readiness:{production.id}")
    assert action.href == f"/production/{production.id}"
    assert action.title == f"Заказ №{production.cycle_id}: недостаточно данных"
    assert action.tone == "warning"

    signal = overview.generate_section_signal(db, user, "production", force=True)
    assert signal.action is not None and signal.action.href == f"/production/{production.id}"
    # Нет заявок — но это не повод писать «всё в порядке».
    assert signal.clear_text is None


def test_shortfall_is_danger_and_provided_gets_clear_text(db, make_user):
    engineer = make_user(Module.PRODUCTION)
    storekeeper = make_user(Module.WAREHOUSE)
    production = _make_production(db)
    material = _add_material(db, _make_block(db, production), required=4)
    db.commit()

    today = overview.generate_today(db, engineer)
    assert next(a for a in today.actions if a.section == "production").tone == "danger"

    request = production_service.request_material(db, material, 4, engineer)
    warehouse_service.approve_request(db, request, storekeeper)
    db.commit()

    signal = overview.generate_section_signal(db, engineer, "production", force=True)
    assert signal.action is None
    assert signal.clear_text == overview.ALL_CLEAR_TEXT["production"]
    widget = next(
        w for w in overview.generate_today(db, engineer).widgets if w.title.startswith("Производств с")
    )
    assert widget.tone == "success"


def test_no_productions_in_work_is_not_praised(db, make_user):
    user = make_user(Module.PRODUCTION)
    signal = overview.generate_section_signal(db, user, "production", force=True)
    assert signal.clear_text == "Производств в работе нет."
    widget = next(w for w in overview.generate_today(db, user).widgets if w.title.startswith("Производств с"))
    assert widget.tone == "neutral"


def test_marina_line_names_state_and_reason(db):
    production = _make_production(db)
    _add_material(db, _make_block(db, production), required=0)
    db.commit()

    _, text = _production_line(_snapshot_production(db))
    assert (
        f"Производство «Заказ №{production.cycle_id}»: недостаточно данных — "
        "Блок «Стены»: у 1 материала не указано количество." in text
    )


# --------------------------------------------------------- ИИ-аналитика --


def _fake_llm(monkeypatch, status=None, error=None, no_tool=False):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        if no_tool:
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="…")])
        return SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", input={"summary": "Всё хорошо.", "status": status},
        )])

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(llm, "anthropic_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    return calls


def test_model_green_is_lowered_to_floor(db, make_user, monkeypatch):
    production = _make_production(db)
    _add_material(db, _make_block(db, production), required=0)
    db.commit()
    calls = _fake_llm(monkeypatch, status="green")

    result = analytics.generate_section_analytics(db, make_user(Module.PRODUCTION), "production", force=True)
    assert result.status == "yellow"
    assert result.source == "ai"
    assert result.status_floor_reason == "Материалы производства: недостаточно данных"
    # Пол и причины переданы модели в запросе.
    assert "Минимальный статус по правилам: yellow" in calls[0]["messages"][0]["content"]
    assert "insufficient_data" in calls[0]["messages"][0]["content"]


def test_shortfall_floor_is_red(db, make_user, monkeypatch):
    production = _make_production(db)
    _add_material(db, _make_block(db, production), required=3)
    db.commit()
    _fake_llm(monkeypatch, status="yellow")

    result = analytics.generate_section_analytics(db, make_user(Module.PRODUCTION), "production", force=True)
    assert result.status == "red"
    assert result.status_floor_reason == "Материалы производства: нехватка материалов"


def test_model_status_worse_than_floor_is_kept(db, make_user, monkeypatch):
    production = _make_production(db)
    _add_material(db, _make_block(db, production), required=0)
    db.commit()
    _fake_llm(monkeypatch, status="red")

    result = analytics.generate_section_analytics(db, make_user(Module.PRODUCTION), "production", force=True)
    assert result.status == "red"
    assert result.status_floor_reason is None


def test_without_llm_production_gets_rules_status(api, make_user, db):
    production = _make_production(db)
    _add_material(db, _make_block(db, production), required=0)
    db.commit()
    user = make_user(Module.PRODUCTION, Module.AI)

    resp = api(user).get("/api/ai/production/analytics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "rules"
    assert body["status"] == "yellow"
    assert "не указано количество" in body["summary"]
    # Ответ по правилам не кешируется — вернётся ИИ, спросим его.
    assert "section_analytics:production" not in _cached_keys(db)


def test_without_llm_other_section_is_unknown_not_error(api, make_user, db):
    user = make_user(Module.CLIENTS, Module.AI)
    resp = api(user).get("/api/ai/clients/analytics")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unknown"
    assert resp.json()["summary"] == "ИИ недоступен, оценка не выполнена."


def test_llm_failure_or_no_tool_use_falls_back_to_rules(db, make_user, monkeypatch):
    production = _make_production(db)
    _add_material(db, _make_block(db, production), required=0)
    db.commit()
    user = make_user(Module.PRODUCTION)

    _fake_llm(monkeypatch, error=RuntimeError("503 overloaded"))
    result = analytics.generate_section_analytics(db, user, "production", force=True)
    assert (result.source, result.status) == ("rules", "yellow")

    _fake_llm(monkeypatch, no_tool=True)
    result = analytics.generate_section_analytics(db, user, "production", force=True)
    assert (result.source, result.status) == ("rules", "yellow")


# ------------------------------------------------------------- сброс кэша --


def test_material_changes_drop_production_caches(db, make_user):
    engineer = make_user(Module.PRODUCTION)
    storekeeper = make_user(Module.WAREHOUSE)
    production = _make_production(db)
    block = _make_block(db, production)
    material = _add_material(db, block, required=0)
    db.commit()
    production_keys = set(PRODUCTION_KEYS + [f"production_home_deadlines:{production.id}"])

    _warm_caches(db, production.id)
    production_service.update_required_quantity(db, material, 5, engineer)
    db.commit()
    assert not production_keys & _cached_keys(db)

    _warm_caches(db, production.id)
    request = production_service.request_material(db, material, 5, engineer)
    db.commit()
    assert not production_keys & _cached_keys(db)

    _warm_caches(db, production.id)
    warehouse_service.approve_request(db, request, storekeeper)
    db.commit()
    assert not production_keys & _cached_keys(db)

    _warm_caches(db, production.id)
    production_service.add_block_material(db, block.id, SimpleNamespace(
        warehouse_material_id=material.warehouse_material_id, inventory_number="", unit="шт", quantity_required=1,
    ))
    db.commit()
    assert not production_keys & _cached_keys(db)


def test_requires_materials_and_block_task_close_drop_caches(api, make_user, db):
    production = _make_production(db)
    block = _make_block(db, production)
    task = task_service.create_task(
        db, title="Сопоставить со складом материал «Брус» (м3) и добавить в блок «Стены»",
        block_id=block.id, link_type=TaskLinkType.BLOCK_MATERIAL_MATCH, link_id=block.id,
    )
    other = _make_production(db)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    _warm_caches(db, production.id)
    ai_cache.set(db, f"production_home_deadlines:{other.id}", {"stale": True}, datetime.now(timezone.utc))
    assert worker.patch(f"/api/production/blocks/{block.id}", json={"requires_materials": False}).status_code == 200
    keys = _cached_keys(db)
    assert not set(PRODUCTION_KEYS) & keys
    assert f"production_home_deadlines:{production.id}" not in keys
    # «Сроки» другого дома не трогаем.
    assert f"production_home_deadlines:{other.id}" in keys

    _warm_caches(db, production.id)
    task_service.force_close(db, task)
    db.commit()
    assert not set(PRODUCTION_KEYS) & _cached_keys(db)


def test_home_deadlines_carry_generated_at(api, make_user, db):
    production = _make_production(db)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    deadlines = worker.get(f"/api/production/{production.id}/home").json()["deadlines"]
    assert deadlines["title"] == "Прогноз не построен"
    assert deadlines["generated_at"] is not None
