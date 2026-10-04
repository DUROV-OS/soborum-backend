"""Код нового материала (0096):

- код по умолчанию — «первое слово названия-номер позиции на складе»;
- знаки препинания по краям слова отбрасываются, пустое название даёт `MAT`;
- номер считается по выбранному складу и пропускает уже занятые коды;
- дубликат кода на том же складе при создании — 409, а не ошибка БД;
- тот же код на другом складе допустим.
"""

from app.common.module_access import AccessLevel, Module
from app.warehouse.models import Warehouse, WarehouseMaterial

TECH = "Склад Технология"


def _material(db, code, warehouse=Warehouse(TECH)):
    db.add(WarehouseMaterial(warehouse=warehouse, title=code, code=code, unit="шт."))
    db.flush()
    db.commit()


def _suggest(client, title, warehouse=TECH):
    resp = client.get("/api/warehouse/materials/suggest-code", params={"warehouse": warehouse, "title": title})
    assert resp.status_code == 200
    return resp.json()["code"]


def test_suggest_code_first_word_and_position(api, make_user, db):
    _material(db, "00001542")
    _material(db, "00001540")
    worker = api(make_user(Module.WAREHOUSE))
    assert _suggest(worker, "Коронка (для металла Matrix Bi-Metall D68 мм") == "Коронка-3"
    assert _suggest(worker, "  «Брус», 150×50") == "Брус-3"
    assert _suggest(worker, "") == "MAT-3"


def test_suggest_code_counts_only_selected_warehouse(api, make_user, db):
    _material(db, "A-1")
    other = next(w for w in Warehouse if w.value != TECH)
    worker = api(make_user(Module.WAREHOUSE))
    assert _suggest(worker, "Брус", warehouse=other.value) == "Брус-1"


def test_suggest_code_skips_taken_codes(api, make_user, db):
    _material(db, "Брус-2")
    worker = api(make_user(Module.WAREHOUSE))
    assert _suggest(worker, "Брус доска") == "Брус-3"


def test_suggest_code_fits_column_length(api, make_user):
    worker = api(make_user(Module.WAREHOUSE))
    code = _suggest(worker, "Ж" * 100)
    assert len(code) == 64
    assert code.endswith("-1")


def test_create_duplicate_code_is_conflict(api, make_user, db):
    _material(db, "Брус-1")
    worker = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))
    body = {"warehouse": TECH, "title": "Брус", "code": "Брус-1", "unit": "шт."}
    resp = worker.post("/api/warehouse/materials", json=body)
    assert resp.status_code == 409
    assert "Брус-1" in resp.json()["detail"]

    other = next(w for w in Warehouse if w.value != TECH)
    resp = worker.post("/api/warehouse/materials", json={**body, "warehouse": other.value})
    assert resp.status_code == 201
