"""Характеристики материала (0078):

- материал заводится с видом/размером/диаметром/серийным номером/количеством в
  упаковке и поставщиком из справочника снабжения;
- характеристики необязательны — материал без них заводится как раньше;
- характеристики правятся и очищаются из карточки материала (PATCH, `null`);
- `pack_quantity <= 0` отклоняется, несуществующий поставщик — 404;
- справочник единиц измерения отдаётся отдельным эндпоинтом.
"""

from app.common.module_access import AccessLevel, Module
from app.warehouse.models import Supplier


def _supplier(db, name="ООО Лесторг"):
    supplier = Supplier(name=name, categories=[], contacts=[])
    db.add(supplier)
    db.flush()
    db.commit()
    return supplier


def _payload(**extra):
    body = {
        "warehouse": "Склад Технология",
        "title": "Рубероид",
        "code": "RUB-1",
        "unit": "рулон",
    }
    body.update(extra)
    return body


def test_material_units_reference(api, make_user):
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.get("/api/warehouse/material-units")
    assert resp.status_code == 200
    assert resp.json() == ["шт.", "рулон", "палета", "кв. м", "куб. м", "пог.м"]


def test_create_material_with_characteristics(api, make_user, db):
    supplier = _supplier(db)
    worker = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))
    resp = worker.post(
        "/api/warehouse/materials",
        json=_payload(
            kind="кровельный",
            size="10×1 м",
            diameter="30 мм",
            serial_number="SN-42",
            pack_quantity=12,
            supplier_id=supplier.id,
        ),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "кровельный"
    assert body["size"] == "10×1 м"
    assert body["diameter"] == "30 мм"
    assert body["serial_number"] == "SN-42"
    assert body["pack_quantity"] == 12
    assert body["supplier_id"] == supplier.id
    assert body["supplier_name"] == "ООО Лесторг"

    card = worker.get(f"/api/warehouse/materials/{body['id']}").json()
    assert card["supplier_name"] == "ООО Лесторг"
    assert card["pack_quantity"] == 12


def test_create_material_without_characteristics(api, make_user):
    worker = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))
    resp = worker.post("/api/warehouse/materials", json=_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] is None
    assert body["pack_quantity"] is None
    assert body["supplier_id"] is None
    assert body["supplier_name"] is None


def test_pack_quantity_must_be_positive(api, make_user):
    worker = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))
    resp = worker.post("/api/warehouse/materials", json=_payload(pack_quantity=0))
    assert resp.status_code == 422


def test_unknown_supplier_rejected(api, make_user):
    worker = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))
    resp = worker.post("/api/warehouse/materials", json=_payload(supplier_id=987654))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Поставщик не найден"


def test_patch_updates_characteristics(api, make_user, db):
    supplier = _supplier(db, name="ИП Кровля")
    worker = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))
    material_id = worker.post("/api/warehouse/materials", json=_payload()).json()["id"]

    resp = worker.patch(
        f"/api/warehouse/materials/{material_id}",
        json={"kind": "мембрана", "supplier_id": supplier.id},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "мембрана"
    assert resp.json()["supplier_name"] == "ИП Кровля"

    bad = worker.patch(f"/api/warehouse/materials/{material_id}", json={"supplier_id": 987654})
    assert bad.status_code == 404


def test_characteristics_can_be_cleared(api, make_user, db):
    supplier = _supplier(db, name="ТД Метиз")
    worker = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))
    material_id = worker.post(
        "/api/warehouse/materials",
        json=_payload(kind="метизы", pack_quantity=100, supplier_id=supplier.id),
    ).json()["id"]

    resp = worker.patch(
        f"/api/warehouse/materials/{material_id}",
        json={"kind": None, "pack_quantity": None, "supplier_id": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] is None
    assert body["pack_quantity"] is None
    assert body["supplier_id"] is None
    assert body["supplier_name"] is None
