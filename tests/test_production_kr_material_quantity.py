"""Норматив количества материала на дом из КР типового проекта (0088-e):

- ИИ при генерации шаблона указывает норматив только явным положительным
  числом; пусто/ноль/текст — «в КР не найдено» (NULL), а не 0;
- страницы спецификации уходят ИИ с увеличенным лимитом символов;
- норматив правится PATCH-ем до подтверждения и переносится в дом при
  разворачивании шаблона;
- «Заполнить нормативы из КР» дозаполняет только пустые нормативы, в том числе
  у подтверждённого шаблона, ничего другого не меняет; без ключа ИИ — 422.
"""

import copy

from app.clients.models import Client
from app.core.config import settings
from app.cycle.models import Cycle, CycleStatus
from app.production import stage_plan, stage_template_service
from app.production.models import BlockMaterial, KrExtraction, Production
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial

GRAPH = {
    "blocks": [
        {
            "name": "Каркас",
            "sequence": 1,
            "depends_on_sequence": [],
            "kr_page_refs": [{"page_number": 2}],
            "tasks": [],
            "materials": [
                {"name": "Брус 150х150", "unit": "шт", "quantity": 36, "kr_page_ref": {"page_number": 2}},
                {"name": "Саморез", "unit": "шт", "quantity": "по месту", "kr_page_ref": {"page_number": 2}},
                {"name": "Утеплитель", "unit": "м2", "quantity": 0, "kr_page_ref": {"page_number": 2}},
                {"name": "Мембрана", "unit": "м2", "kr_page_ref": {"page_number": 2}},
            ],
        }
    ]
}


def _client_with_kr(db, texts):
    cycle = Cycle()
    db.add(cycle)
    db.flush()
    client = Client(cycle_id=cycle.id, full_name="Клиент КР", phone="+79160000001", email="kr@example.com")
    db.add(client)
    db.flush()
    pages = [{"page_number": i + 1, "text": t, "image_file_id": 1} for i, t in enumerate(texts)]
    db.add(KrExtraction(client_id=client.id, pages=pages))
    db.commit()
    return client


def _generate(db, monkeypatch, graph=GRAPH, texts=("общие данные", "спецификация каркаса")):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    seen = {}

    def fake_call_ai(pages_payload):
        seen["pages"] = pages_payload
        return copy.deepcopy(graph)

    monkeypatch.setattr(stage_template_service, "_call_ai", fake_call_ai)
    client = _client_with_kr(db, list(texts))
    template = stage_template_service.generate_or_reuse_template(db, client)
    db.commit()
    return template, seen


def _materials(template):
    return {m.name: m for block in template.blocks for m in block.materials}


def test_generation_keeps_only_explicit_positive_quantities(db, monkeypatch):
    template, _ = _generate(db, monkeypatch)
    materials = _materials(template)
    assert materials["Брус 150х150"].quantity == 36
    assert materials["Саморез"].quantity is None
    assert materials["Утеплитель"].quantity is None
    assert materials["Мембрана"].quantity is None


def test_spec_pages_get_larger_text_limit(db, monkeypatch):
    long_tail = "х" * 5000
    _, seen = _generate(db, monkeypatch, texts=("общие данные " + long_tail, "Спецификация материалов " + long_tail))
    by_page = {p["page_number"]: p["text"] for p in seen["pages"]}
    assert len(by_page[1]) == 2000
    assert len(by_page[2]) > 5000


def test_patch_quantity_and_transfer_to_house(api, make_user, db, monkeypatch):
    template, _ = _generate(db, monkeypatch)
    admin = api(make_user(admin=True))
    materials = _materials(template)
    block = template.blocks[0]

    # Сопоставляем со складом, чтобы материалы попали в дом как BlockMaterial.
    warehouse_ids = {}
    for name in ("Брус 150х150", "Саморез"):
        wm = WarehouseMaterial(
            warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title=name, code=f"C-{name}", unit="шт",
        )
        db.add(wm)
        db.flush()
        warehouse_ids[name] = wm.id
    db.commit()

    base = f"/api/production/stage-templates/{template.id}/blocks/{block.id}/materials"
    assert admin.patch(f"{base}/{materials['Брус 150х150'].id}", json={"warehouse_material_id": warehouse_ids["Брус 150х150"]}).status_code == 200
    resp = admin.patch(
        f"{base}/{materials['Саморез'].id}",
        json={"warehouse_material_id": warehouse_ids["Саморез"], "quantity": 120},
    )
    assert resp.status_code == 200, resp.text
    saved = {m["name"]: m["quantity"] for m in resp.json()["blocks"][0]["materials"]}
    assert saved["Саморез"] == 120
    assert admin.patch(f"{base}/{materials['Саморез'].id}", json={"quantity": -1}).status_code == 422

    assert admin.post(f"/api/production/stage-templates/{template.id}/confirm").status_code == 200
    db.expire_all()
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    stage_plan.instantiate_stage_plan(db, production, db.get(type(template), template.id))
    db.commit()

    required = {
        bm.warehouse_material_id: float(bm.quantity_required) for bm in db.query(BlockMaterial).all()
    }
    assert required == {warehouse_ids["Брус 150х150"]: 36, warehouse_ids["Саморез"]: 120}


def test_fill_quantities_only_fills_missing_even_when_confirmed(api, make_user, db, monkeypatch):
    template, _ = _generate(db, monkeypatch)
    admin = api(make_user(admin=True))
    assert admin.post(f"/api/production/stage-templates/{template.id}/confirm").status_code == 200
    materials = _materials(template)
    beam, screws, wool, membrane = (materials[n] for n in ("Брус 150х150", "Саморез", "Утеплитель", "Мембрана"))

    asked = []

    def fake_quantities(pages_payload, batch):
        asked.extend(item["material_id"] for item in batch)
        return [
            {"material_id": beam.id, "quantity": 999, "kr_page_ref": {"page_number": 2}},  # уже есть — не трогать
            {"material_id": screws.id, "quantity": 480, "kr_page_ref": {"page_number": 5}},
            {"material_id": wool.id, "quantity": "нет данных", "kr_page_ref": {"page_number": 2}},
            {"material_id": 10_000, "quantity": 7, "kr_page_ref": {"page_number": 2}},  # чужой id
        ]

    monkeypatch.setattr(stage_template_service, "_call_ai_quantities", fake_quantities)
    resp = admin.post(f"/api/production/stage-templates/{template.id}/fill-quantities")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["filled"], body["remaining"]) == (1, 2)
    assert body["template"]["status"] == "confirmed"
    assert sorted(asked) == sorted([screws.id, wool.id, membrane.id])

    db.expire_all()
    assert (db.get(type(beam), beam.id).quantity, db.get(type(screws), screws.id).quantity) == (36, 480)
    assert db.get(type(wool), wool.id).quantity is None
    assert db.get(type(membrane), membrane.id).quantity is None


def test_fill_quantities_without_api_key(api, make_user, db, monkeypatch):
    template, _ = _generate(db, monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    admin = api(make_user(admin=True))
    resp = admin.post(f"/api/production/stage-templates/{template.id}/fill-quantities")
    assert resp.status_code == 422
