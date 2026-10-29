from copy import deepcopy
import pytest
from fastapi import HTTPException
from app.moysklad.importer import import_export
from app.moysklad.models import SourceRecord, SourceLink
from app.moysklad import service
from app.common.module_access import Module
from app.accounting.models import Counterparty, MoneyMovement, MoneyMovementStatus
from app.warehouse.models import WarehouseMaterial, Warehouse, MaterialCategory, StockMovement
from app.users.models import User

BASE = "https://api.moysklad.ru/api/remap/1.2/entity/"


def ref(typ, id):
    return {"meta": {"href": BASE + typ + "/" + id, "type": typ}}


@pytest.fixture
def snapshot():
    data = {
        "organization": [
            {
                "id": "o",
                "name": "ООО «Технология»",
                "inn": "1234567890",
                "accounts": {"rows": [{"id": "a", "accountNumber": "123", "bankName": "Банк"}]},
            }
        ],
        "store": [{"id": "s", "name": Warehouse.TECHNOLOGY.value}],
        "product": [
            {
                "id": "p",
                "name": "Архивный брус",
                "code": "BRUS",
                "archived": True,
                "uom": {"name": "м³"},
                "buyPrice": {"value": 12345},
            }
        ],
        "counterparty": [{"id": "c", "name": "Архивный поставщик", "archived": True}],
        "paymentout": [
            {
                "id": "pay",
                "name": "42",
                "sum": 234567,
                "vatSum": 1234,
                "applicable": True,
                "moment": "2026-10-01 12:00:00",
                "organization": ref("organization", "o"),
                "organizationAccount": ref("account", "a"),
                "agent": ref("counterparty", "c"),
            }
        ],
        "employee": [{"id": "employee", "name": "Архивный сотрудник", "archived": True, "salary": 10000000}],
        "processingplan": [{"id": "bom", "name": "Архивная техкарта", "archived": True}],
    }
    reports = {
        "report/stock/bystore": [
            {"meta": {"href": BASE + "product/p"}, "stockByStore": [{**ref("store", "s"), "stock": 2.5}]}
        ]
    }
    manifest = {"completed_at": "2026-10-28T12:00:00+00:00", "counts": {k: len(v) for k, v in data.items()}}
    return data, {}, reports, manifest


def test_import_preserves_archive_money_and_existing_material(db, make_user, snapshot):
    owner = make_user(admin=True)
    users_before = db.query(User).count()
    existing = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY,
        title="Архивный брус",
        code="BRUS",
        unit="м³",
        category=MaterialCategory.BEAMS_BOARDS,
        quantity_in_stock=8,
        purchase_price=0,
        threshold=3,
    )
    db.add(existing)
    db.commit()
    result = import_export(db, *snapshot, owner.id)
    db.commit()
    assert db.query(WarehouseMaterial).count() == 1
    assert existing.quantity_in_stock == 2.5 and existing.is_archived
    assert existing.threshold == 3 and existing.category == MaterialCategory.BEAMS_BOARDS
    assert float(existing.purchase_price) == 123.45
    assert db.query(Counterparty).one().is_active is False
    payment = db.query(MoneyMovement).one()
    assert float(payment.amount) == 2345.67 and float(payment.tax) == 12.34
    assert payment.status == MoneyMovementStatus.POSTED
    assert db.query(SourceRecord).filter_by(entity_type="employee").one().archived
    assert db.query(SourceRecord).filter_by(entity_type="processingplan").one().archived
    assert db.query(User).count() == users_before
    assert db.query(StockMovement).one().delta == -5.5
    assert not result["warnings"]


def test_retry_does_not_duplicate_or_reset_local_stock(db, make_user, snapshot):
    owner = make_user(admin=True)
    import_export(db, *snapshot, owner.id)
    db.commit()
    material = db.query(WarehouseMaterial).one()
    material.quantity_in_stock = 1
    db.commit()
    links = db.query(SourceLink).count()
    records = db.query(SourceRecord).count()
    result = import_export(db, *deepcopy(snapshot), owner.id)
    db.commit()
    assert result["native_replay_skipped"]
    assert material.quantity_in_stock == 1
    assert db.query(MoneyMovement).count() == 1
    assert db.query(SourceRecord).count() == records and db.query(SourceLink).count() == links


def test_missing_stock_export_fails_before_mutation(db, make_user, snapshot):
    owner = make_user(admin=True)
    data, meta, _, manifest = snapshot
    with pytest.raises(ValueError, match="stock report"):
        import_export(db, data, meta, {}, manifest, owner.id)
    assert db.query(SourceRecord).count() == 0


def test_access_archive_search_and_api(db, make_user, api, snapshot):
    owner = make_user(admin=True)
    import_export(db, *snapshot, owner.id)
    db.commit()
    warehouse = make_user(Module.WAREHOUSE)
    result = service.search(db, warehouse, "брус", archive="archived")
    assert result["total"] == 1 and result["items"][0]["archived"]
    assert service.search(db, warehouse, "сотрудник", archive="all")["total"] == 0
    employee = db.query(SourceRecord).filter_by(entity_type="employee").one()
    with pytest.raises(HTTPException) as e:
        service.get_record(db, warehouse, employee.id)
    assert e.value.status_code == 403
    client = api(warehouse)
    assert client.get("/api/moysklad/records?archive=archived").status_code == 200
    assert client.get("/api/moysklad/records?entity_type=employee").status_code == 403
    assert client.get("/api/moysklad/records/" + str(employee.id)).status_code == 403
    assert all(c["type"] != "employee" for c in client.get("/api/moysklad/catalog").json()["categories"])
