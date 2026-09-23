"""Единый справочник контрагентов (задача 0081-c).

До этой задачи платёж можно было привязать только к клиенту, сотруднику или
заказу поставщику; налоговая, арендодатель и разовый подрядчик оставались без
привязки, и история платежей по контрагенту не собиралась. Проверяем сам
справочник, сопоставление по наименованию/ИНН и историю.
"""

import pytest

from app.accounting.models import Counterparty, CounterpartyKind
from app.accounting.seed import ensure_counterparties_seed
from app.accounting import service as accounting_service
from app.clients import service as client_service
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.warehouse.models import Supplier


@pytest.fixture
def acc_user(make_user):
    return make_user(Module.ACCOUNTING)


def _client(db, name="Иванов Иван"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def _supplier(db, name="ООО «Брус»"):
    supplier = Supplier(name=name, categories=[], contacts=[])
    db.add(supplier)
    db.commit()
    return supplier


def _create_movement(api_client, counterparty_id, **overrides):
    body = {"subkind": "other_expense", "amount": 1000, "counterparty_id": counterparty_id}
    body.update(overrides)
    return api_client.post("/api/accounting/money-movements", json=body)


def _post(api_client, mm_id):
    api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "approved"})
    return api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "posted"})


def test_seed_mirrors_clients_and_suppliers_and_is_idempotent(db):
    _client(db, "Петров Пётр")
    _supplier(db, "ООО «Метизы»")

    created = ensure_counterparties_seed(db)
    assert created == 2
    kinds = {c.name: c.kind for c in db.query(Counterparty).all()}
    assert kinds["Петров Пётр"] is CounterpartyKind.CLIENT
    assert kinds["ООО «Метизы»"] is CounterpartyKind.SUPPLIER

    assert ensure_counterparties_seed(db) == 0
    assert db.query(Counterparty).count() == 2


def test_create_counterparty_without_source_and_reject_empty_name(db, api, acc_user):
    api_client = api(acc_user)
    resp = api_client.post(
        "/api/accounting/counterparties",
        json={"name": "ИФНС №7", "inn": "7707083893", "kind": "government"},
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "government"

    assert api_client.post("/api/accounting/counterparties", json={"name": "   "}).status_code == 422


def test_duplicate_inn_is_rejected(db, api, acc_user):
    api_client = api(acc_user)
    api_client.post("/api/accounting/counterparties", json={"name": "ООО «Ромашка»", "inn": "7701234567"})
    second = api_client.post(
        "/api/accounting/counterparties", json={"name": "Ромашка (старое)", "inn": "7701234567"}
    )
    assert second.status_code == 409


def test_counterparty_cannot_be_client_and_supplier_at_once(db, api, acc_user):
    client = _client(db)
    supplier = _supplier(db)
    resp = api(acc_user).post(
        "/api/accounting/counterparties",
        json={"name": "Оба сразу", "client_id": client.id, "supplier_id": supplier.id},
    )
    assert resp.status_code == 422


def test_kind_follows_the_link(db, api, acc_user):
    client = _client(db)
    resp = api(acc_user).post(
        "/api/accounting/counterparties",
        json={"name": client.full_name, "kind": "other", "client_id": client.id},
    )
    # тип выводится из ссылки, а не берётся на веру из запроса
    assert resp.json()["kind"] == "client"


def test_card_aggregates_and_payment_history(db, api, acc_user):
    api_client = api(acc_user)
    cp_id = api_client.post(
        "/api/accounting/counterparties", json={"name": "ООО «Аренда»", "kind": "other"}
    ).json()["id"]

    paid = _create_movement(api_client, cp_id, subkind="rent", amount=130000).json()["id"]
    _post(api_client, paid)
    got = _create_movement(api_client, cp_id, subkind="other_income", amount=45000).json()["id"]
    _post(api_client, got)
    # черновик в агрегаты не идёт: деньги ещё не прошли
    _create_movement(api_client, cp_id, subkind="other_expense", amount=999999)

    card = api_client.get(f"/api/accounting/counterparties/{cp_id}").json()
    assert card["total_expense"] == 130000
    assert card["total_income"] == 45000
    assert card["payments_count"] == 2
    assert card["last_payment_at"] is not None

    history = api_client.get(f"/api/accounting/counterparties/{cp_id}/payments").json()
    # в историю попадают и черновики — это платежи контрагента, просто ещё не проведённые
    assert len(history) == 3
    assert all(p["counterparty_name"] == "ООО «Аренда»" for p in history)

    filtered = api_client.get(
        "/api/accounting/money-movements", params={"counterparty_id": cp_id}
    ).json()
    assert {p["id"] for p in filtered} == {p["id"] for p in history}
    assert {paid, got} <= {p["id"] for p in filtered}


def test_match_or_create_finds_by_normalized_name(db):
    existing = Counterparty(name="ООО «Ромашка»", kind=CounterpartyKind.OTHER)
    db.add(existing)
    db.commit()

    # другой регистр, другие кавычки, без ОПФ — тот же контрагент
    assert accounting_service.match_or_create_counterparty(db, 'ромашка').id == existing.id
    assert accounting_service.match_or_create_counterparty(db, 'ООО "РОМАШКА"').id == existing.id
    assert db.query(Counterparty).count() == 1


def test_match_or_create_prefers_inn_and_backfills_it(db):
    existing = Counterparty(name="ООО «Ромашка»", kind=CounterpartyKind.OTHER)
    db.add(existing)
    db.commit()

    # имя в выписке другое, но ИНН пуст у нас — совпали по имени и дописали ИНН
    matched = accounting_service.match_or_create_counterparty(db, "ООО «Ромашка»", "7701234567")
    db.commit()
    assert matched.id == existing.id
    assert matched.inn == "7701234567"

    # теперь ИНН решает даже при другом наименовании
    renamed = accounting_service.match_or_create_counterparty(db, "Ромашка Торг", "7701234567")
    assert renamed.id == existing.id


def test_match_or_create_creates_unknown_counterparty(db):
    created = accounting_service.match_or_create_counterparty(db, "ИП Сидоров")
    db.commit()
    assert created is not None
    assert created.kind is CounterpartyKind.OTHER
    # без имени и ИНН выдумывать нечего
    assert accounting_service.match_or_create_counterparty(db, "   ") is None


def test_unknown_counterparty_on_movement_is_rejected(db, api, acc_user):
    resp = _create_movement(api(acc_user), 999)
    assert resp.status_code == 422


def test_counterparty_is_deactivated_not_deleted(db, api, acc_user):
    api_client = api(acc_user)
    cp_id = api_client.post("/api/accounting/counterparties", json={"name": "Разовый подрядчик"}).json()["id"]
    api_client.patch(f"/api/accounting/counterparties/{cp_id}", json={"is_active": False})

    assert cp_id not in [c["id"] for c in api_client.get("/api/accounting/counterparties").json()]
    with_inactive = api_client.get(
        "/api/accounting/counterparties", params={"include_inactive": True}
    ).json()
    assert cp_id in [c["id"] for c in with_inactive]
