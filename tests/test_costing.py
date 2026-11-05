from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from app.common.module_access import AccessLevel, Module
from app.costing.models import CostingRevision

PREFIX = "/api/costing"
EXPENSES = {k: "0" for k in ("labor", "transport", "subcontract", "overhead", "commercial", "advertising", "taxes", "reserve")} | {"evidence": "Проверенные условия теста"}


def material(api, name="Доска", price="51"):
    response = api.post(PREFIX + "/materials", json={"name": name, "unit": "м3", "price": price,
        "price_source": "Прайс поставщика", "effective_on": "2026-01-01"})
    assert response.status_code == 201, response.text
    return response.json()


def item(api, mat, kind="template", price="100", replacements=None):
    response = api.post(PREFIX + "/items", json={"kind": kind, "title": kind, "content": {
        "lines": [{"material_id": mat["id"], "quantity": "1"}], "expenses": EXPENSES,
        "retail_price": price, "replacements": replacements or []}})
    assert response.status_code == 201, response.text
    return response.json()


def request(template, **kwargs):
    return {"request_id": str(uuid4()), "title": "Тестовая смета", "template_id": template["id"], **kwargs}


@pytest.fixture
def setup(api, make_user):
    owner = make_user(admin=True)
    client = api(owner)
    mat = material(client)
    template = item(client, mat)
    return client, mat, template, owner


def test_exact_35_percent_and_fraction_below(setup):
    api, mat, template, _ = setup
    # With 19% revenue-dependent expense: 51 + 19 = 70, 30% margin.
    body = request(template, sale_price="110.87")
    response = api.post(PREFIX + "/estimates", json=body)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["result"]["status"] == "meets_target"
    body = request(template, sale_price="110.86")
    result = api.post(PREFIX + "/estimates", json=body).json()
    assert result["result"]["status"] == "below_target"
    assert result["result"]["minimum_price"] == "110.87"
    assert api.post(f'{PREFIX}/estimates/{result["id"]}/approve', json={"version": 1, "request_id": str(uuid4()), "reason": "Проверено"}).status_code == 422


def test_unknown_costs_and_zero_are_distinct(setup):
    api, mat, template, _ = setup
    mat["price"] = None
    assert api.put(f'{PREFIX}/materials/{mat["id"]}', json=mat).status_code == 422  # output-only id forbidden
    body = {k: v for k, v in mat.items() if k != "id"}
    assert api.put(f'{PREFIX}/materials/{mat["id"]}', json=body).status_code == 200
    result = api.post(PREFIX + "/estimates", json=request(template)).json()
    assert result["result"]["status"] == "incomplete"
    assert result["result"]["cost"] is None
    assert "Доска" in " ".join(result["result"]["missing"])


def test_snapshot_does_not_drift_and_reprice_keeps_history(setup, db):
    api, mat, template, _ = setup
    first = api.post(PREFIX + "/estimates", json=request(template)).json()
    original = deepcopy(db.query(CostingRevision).first().snapshot)
    payload = {k: v for k, v in mat.items() if k != "id"}
    payload["price"] = "70"
    assert api.put(f'{PREFIX}/materials/{mat["id"]}', json=payload).status_code == 200
    saved = api.get(f'{PREFIX}/estimates/{first["id"]}').json()
    assert saved["result"] == first["result"]
    edit = request(template, version=1, note="Правка текста")
    second = api.put(f'{PREFIX}/estimates/{first["id"]}', json=edit).json()
    assert second["bom"][0]["price"] == "51.00000000"
    edit.update(version=2, request_id=str(uuid4()), reason="Новые цены")
    third = api.post(f'{PREFIX}/estimates/{first["id"]}/reprice', json=edit).json()
    assert third["version"] == 3
    assert Decimal(third["bom"][0]["price"]) == 70
    assert db.query(CostingRevision).filter_by(number=1).one().snapshot == original
    assert len(api.get(f'{PREFIX}/estimates/{first["id"]}/history').json()) == 3
    edit["request_id"] = str(uuid4())
    assert api.put(f'{PREFIX}/estimates/{first["id"]}', json=edit).status_code == 409


def test_request_replay_and_stale_edit(setup):
    api, mat, template, _ = setup
    body = request(template)
    first = api.post(PREFIX + "/estimates", json=body).json()
    second = api.post(PREFIX + "/estimates", json=body).json()
    assert first["id"] == second["id"]
    body["title"] = "Другие данные"
    assert api.post(PREFIX + "/estimates", json=body).status_code == 409
    assert api.put(f'{PREFIX}/estimates/{first["id"]}', json=request(template, version=2)).status_code == 409


def test_double_replacement_is_rejected_and_bom_balances(setup):
    api, mat, template, _ = setup
    expensive = material(api, "Улучшенная доска", "80")
    addon = item(api, expensive, "addon", "80", [{"material_id": mat["id"], "quantity": "1"}])
    body = request(template, selections=[{"item_id": addon["id"], "quantity": "2"}])
    assert api.post(PREFIX + "/estimates", json=body).status_code == 422
    body["selections"][0]["quantity"] = "1"
    body["request_id"] = str(uuid4())
    result = api.post(PREFIX + "/estimates", json=body).json()
    assert len(result["bom"]) == 1
    assert result["bom"][0]["material_id"] == expensive["id"]
    assert Decimal(result["bom"][0]["quantity"]) == 1


def test_package_discount_and_global_discount_applied_once(setup):
    api, mat, template, _ = setup
    addon = item(api, mat, "addon", "100")
    response = api.post(PREFIX + "/items", json={"kind": "package", "title": "Под ключ", "content": {
        "addons": [{"addon_id": addon["id"], "quantity": "2"}], "discount": "20"}})
    assert response.status_code == 201, response.text
    body = request(template, discount="10", selections=[{"item_id": response.json()["id"], "quantity": "1"}])
    result = api.post(PREFIX + "/estimates", json=body).json()
    assert result["list_price"] == "260.00"
    assert result["price"] == "234.00"
    assert result["result"]["rate_cost"] == "44.46"
    assert len(result["commercial"]) == 2
    body.update(request_id=str(uuid4()), sale_price="234")
    assert api.post(PREFIX + "/estimates", json=body).status_code == 422


def test_commercial_permissions_and_exports(setup, api, make_user):
    owner_api, mat, template, _ = setup
    worker = make_user(Module.CLIENTS)
    worker.job_role = "manager"
    client = api(worker)
    result = client.post(PREFIX + "/estimates", json=request(template)).json()
    assert "result" not in result and "bom" not in result
    assert "profile" not in result["input"]
    for path in ("materials", "items", "journal", "settings", "assistant", "warehouse-catalog", "price-versions"):
        assert client.get(PREFIX + "/" + path).status_code == 403
    assert "expenses" not in str(client.get(PREFIX + "/catalog").json())
    bad = request(template, lines=[{"material_id": mat["id"], "quantity": "1"}])
    assert client.post(PREFIX + "/estimates", json=bad).status_code == 403
    id = result["id"]
    assert client.get(f"{PREFIX}/estimates/{id}/export/xlsx?internal=true").status_code == 403
    other = api(make_user(Module.CLIENTS))
    assert other.get(f"{PREFIX}/estimates/{id}").status_code == 404
    assert other.get(f"{PREFIX}/estimates/{id}/history").status_code == 404
    assert other.get(f"{PREFIX}/estimates/{id}/export/pdf").status_code == 404
    assert other.get(PREFIX + "/estimates").json()["total"] == 0


def test_read_only_cannot_write_and_manager_cannot_gain_costs(api, make_user):
    viewer = make_user(Module.COSTING, level=AccessLevel.VIEW)
    client = api(viewer)
    assert client.get(PREFIX + "/materials").status_code == 200
    assert client.post(PREFIX + "/materials", json={"name": "X", "unit": "шт", "effective_on": "2026-01-01"}).status_code == 403
    viewer.job_role = "manager"
    assert client.get(PREFIX + "/materials").status_code == 403


def test_price_freezing_settings_and_archive(setup):
    api, mat, template, _ = setup
    response = api.post(PREFIX + "/prices", json={"reason": "Новый прайс", "items": [{"id": template["id"], "version": 1, "price": "150"}]})
    assert response.status_code == 200, response.text
    assert api.post(PREFIX + "/prices", json={"reason": "Старая версия", "items": [{"id": template["id"], "version": 1, "price": "200"}]}).status_code == 409
    assert api.post(PREFIX + "/price-versions", json={"title": "Ноябрь"}).status_code == 201
    settings = api.get(PREFIX + "/settings").json()
    settings["content"]["profiles"][0]["margin"] = "40"
    assert api.put(PREFIX + "/settings", json=settings).status_code == 200
    assert api.get(PREFIX + "/items").json()[0]["calculation"]["price"] == "150.00"
    assert len(api.get(PREFIX + "/price-versions/1").json()["items"]) == 1


def test_area_modules_and_half_up(setup):
    api, mat, template, _ = setup
    data = template["content"]
    data.update(mode="area", modules=[{"length": "3", "width": "2", "count": 2}], retail_rate="100", retail_price=None)
    payload = {"version": 1, "kind": "template", "title": "Модульный", "content": data}
    result = api.put(f'{PREFIX}/items/{template["id"]}', json=payload).json()
    assert result["calculation"]["area"] == "12"
    assert result["calculation"]["price"] == "1000.00"  # final 1000 step
    from app.costing.service import rounded
    assert rounded(Decimal("1250"), "500") == 1500


def test_pdf_xlsx_and_formula_injection(setup):
    api, mat, template, _ = setup
    result = api.post(PREFIX + "/estimates", json=request(template, title="=1+1")).json()
    id = result["id"]
    xlsx = api.get(f"{PREFIX}/estimates/{id}/export/xlsx")
    assert xlsx.status_code == 200
    from openpyxl import load_workbook
    wb = load_workbook(BytesIO(xlsx.content))
    assert not any(c.data_type == "f" for row in wb.active for c in row)
    pdf = api.get(f"{PREFIX}/estimates/{id}/export/pdf")
    assert pdf.status_code == 200, pdf.text[:200] if not pdf.content.startswith(b"%PDF") else ""
    import fitz
    doc = fitz.open(stream=pdf.content, filetype="pdf")
    text = "".join(p.get_text() for p in doc)
    assert "Итого" in text and "100.00" in text
    assert "закупки" not in text and "Доска" not in text


def test_import_is_atomic(setup, db):
    api, mat, template, _ = setup
    from app.costing.exports import workbook
    raw = workbook(["Наименование", "Категория", "Единица", "Цена", "Действует с", "Источник"], [
        ["Материал 1", "", "шт", "10", "2026-01-01", "Прайс"],
        ["", "", "шт", "10", "2026-01-01", "Прайс"]], "Справочник")
    # Validation must happen before the first write, including errors in later rows.
    from app.costing.models import CostingMaterial
    before = db.query(CostingMaterial).count()
    response = api.post(PREFIX + "/materials/import", files={"file": ("test.xlsx", raw)})
    assert response.status_code == 422
    assert db.query(CostingMaterial).count() == before


def test_copy_is_frozen_and_replay_safe(setup):
    api, mat, template, _ = setup
    original = api.post(PREFIX + "/estimates", json=request(template)).json()
    payload = {k: v for k, v in mat.items() if k != "id"}
    payload["price"] = "90"
    api.put(f'{PREFIX}/materials/{mat["id"]}', json=payload)
    body = {"version": 1, "request_id": str(uuid4()), "reason": "Копия для варианта"}
    copied = api.post(f'{PREFIX}/estimates/{original["id"]}/copy', json=body).json()
    assert copied["id"] != original["id"]
    assert copied["result"] == original["result"]
    replay = api.post(f'{PREFIX}/estimates/{original["id"]}/copy', json=body).json()
    assert replay["id"] == copied["id"]
    assert api.get(PREFIX + "/estimates").json()["total"] == 2


def test_approval_revoked_on_edit_and_old_metadata_preserved(setup):
    api, mat, template, _ = setup
    original = api.post(PREFIX + "/estimates", json=request(template, sale_price="150")).json()
    action = {"version": 1, "request_id": str(uuid4()), "reason": "Проверено"}
    assert api.post(f'{PREFIX}/estimates/{original["id"]}/approve', json=action).json()["status"] == "approved"
    edited = request(template, version=1, title="Новое имя", sale_price="150")
    assert api.put(f'{PREFIX}/estimates/{original["id"]}', json=edited).json()["status"] == "draft"
    old = api.get(f'{PREFIX}/estimates/{original["id"]}?revision=1').json()
    assert old["title"] == original["title"]
    action.update(version=2, request_id=str(uuid4()), archived=True)
    assert api.post(f'{PREFIX}/estimates/{original["id"]}/archive', json=action).json()["archived"] is True
    assert api.post(f'{PREFIX}/estimates/{original["id"]}/archive', json=action).json()["archived"] is True


def test_package_freeze_and_named_price_restore(setup):
    api, mat, template, _ = setup
    addon = item(api, mat, "addon", "100")
    package = api.post(PREFIX + "/items", json={"kind": "package", "title": "Пакет", "content": {
        "addons": [{"addon_id": addon["id"], "quantity": "2"}], "discount": "20"}}).json()
    frozen = api.post(f'{PREFIX}/items/{package["id"]}/freeze-package', json={"version": 1, "request_id": str(uuid4()), "reason": "Акция"})
    assert frozen.status_code == 200, frozen.text
    version = api.post(PREFIX + "/price-versions", json={"title": "До изменения"}).json()
    assert api.post(PREFIX + "/prices", json={"reason": "Повышение", "items": [{"id": addon["id"], "version": 1, "price": "200"}]}).status_code == 200
    assert api.get(PREFIX + "/items?kind=package").json()[0]["calculation"]["price"] == "160.00"
    estimate = api.post(PREFIX + "/estimates", json=request(template, selections=[{"item_id": package["id"], "quantity": "1"}])).json()
    assert estimate["price"] == "260.00"
    body = {"reason": "Возврат прайса", "versions": {str(template["id"]): 1, str(addon["id"]): 2, str(package["id"]): 2}}
    assert api.post(f'{PREFIX}/price-versions/{version["id"]}/restore', json=body).status_code == 200
    assert api.get(PREFIX + "/items?kind=addon").json()[0]["calculation"]["price"] == "100.00"
    assert api.post(f'{PREFIX}/price-versions/{version["id"]}/restore', json=body).status_code == 409


def test_linked_material_uses_shared_exact_warehouse_price_and_permissions(api, make_user, db):
    from app.warehouse.models import WarehouseMaterial, Warehouse, MaterialCategory
    stock = WarehouseMaterial(title="Общий материал", code="COST-TEST", warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE, unit="шт", purchase_price=Decimal("51.12345678"), quantity_in_stock=10)
    db.add(stock)
    db.commit()
    cost_user = make_user(Module.COSTING)
    client = api(cost_user)
    body = {"warehouse_material_id": stock.id, "name": "Общий материал", "unit": "шт", "price": None, "effective_on": "2026-01-01"}
    response = client.post(PREFIX + "/materials", json=body)
    assert response.status_code == 201, response.text
    material = response.json()
    assert Decimal(material["price"]) == Decimal("51.12345678")
    body.update(version=1, price="52", price_source="Новый прайс")
    assert client.put(f'{PREFIX}/materials/{material["id"]}', json=body).status_code == 403
    editor = api(make_user(Module.COSTING, Module.WAREHOUSE))
    assert editor.put(f'{PREFIX}/materials/{material["id"]}', json=body).status_code == 200
    assert Decimal(str(db.get(WarehouseMaterial, stock.id).purchase_price)) == 52
    stock.purchase_price = 61
    db.commit()
    assert Decimal(editor.get(PREFIX + "/materials").json()[0]["price"]) == 61
    assert stock.quantity_in_stock == 10


def test_future_price_and_infeasible_profile_cannot_pass(setup):
    api, mat, template, _ = setup
    payload = {k: v for k, v in mat.items() if k != "id"}
    payload["effective_on"] = "2200-01-01"
    api.put(f'{PREFIX}/materials/{mat["id"]}', json=payload)
    assert api.post(PREFIX + "/estimates", json=request(template)).json()["result"]["status"] == "incomplete"
    payload = {"version": 1, "kind": "template", "title": "Невозможная маржа", "content": {**template["content"], "margin": "95"}}
    assert api.put(f'{PREFIX}/items/{template["id"]}', json=payload).status_code == 422


def test_fractional_commercial_lines_sum_to_invoice_total(setup):
    api, mat, template, _ = setup
    addon = item(api, mat, "addon", "0.01")
    body = request(template, quantity="0.5", selections=[{"item_id": addon["id"], "quantity": "1"}])
    result = api.post(PREFIX + "/estimates", json=body).json()
    assert sum(Decimal(line["amount"]) for line in result["commercial"]) == Decimal(result["list_price"])


def test_costing_migration_matches_models(db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect
    from app.db.base import Base
    import importlib.util
    from pathlib import Path
    filename = Path(__file__).resolve().parents[1] / "alembic/versions/ce0511202601_costing.py"
    spec = importlib.util.spec_from_file_location("costing_migration_test", filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            inspector = inspect(connection)
            for name, table in Base.metadata.tables.items():
                if name.startswith("costing_"):
                    assert set(table.columns.keys()) == {c["name"] for c in inspector.get_columns(name)}
            migration.downgrade()
            assert not any(n.startswith("costing_") for n in inspect(connection).get_table_names())
    engine.dispose()
