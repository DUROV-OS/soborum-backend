"""Реестр движения денег (app.accounting) — задача 0011-c.

Модель повторяет МойСклад: `posted` ⇔ «проведён» (applicable=true),
статусная машина только вперёд + отмена, полиморфная привязка source_ref
ровно к одному источнику. Сшивку с clients/users/supplies делает 0011-f —
здесь проверяем только реестр и переходы.
"""

import pytest

from app.accounting.models import SupplierOrder
from app.clients import service as client_service
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.warehouse.models import Supplier


@pytest.fixture
def acc_user(make_user):
    return make_user(Module.ACCOUNTING)


@pytest.fixture
def other_user(make_user):
    return make_user(Module.WAREHOUSE)


def _client(db, name="Клиент Тест"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def _supply(db, user):
    supplier = Supplier(name="ООО Брус", categories=[], contacts=[])
    db.add(supplier)
    db.flush()
    order = SupplierOrder(
        supplier_id=supplier.id,
        items=[{"material": "Брус", "category": None, "quantity": 10, "unit_price": 5000}],
        total_cost=50000,
    )
    db.add(order)
    db.commit()
    return order


def _create(api_client, **overrides):
    body = {"subkind": "sale_income", "amount": 100000, "tax": 20000}
    body.update(overrides)
    return api_client.post("/api/accounting/money-movements", json=body)


def test_create_sale_income_is_draft_and_bound_to_client(db, api, acc_user):
    client = _client(db)
    resp = _create(api(acc_user), subkind="sale_income", client_id=client.id)
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "draft"
    assert data["direction"] == "income"
    assert data["source_kind"] == "client"
    assert data["client_id"] == client.id
    assert data["source_label"] == client.full_name
    assert data["initiator_id"] == acc_user.id


def test_status_machine_no_skipping_and_posted_is_frozen(db, api, acc_user):
    client = _client(db)
    api_client = api(acc_user)
    mm_id = _create(api_client, client_id=client.id).json()["id"]

    # draft -> posted напрямую запрещён
    r = api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "posted"})
    assert r.status_code == 409

    assert api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status", json={"to": "approved"}
    ).status_code == 200
    posted = api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status", json={"to": "posted"}
    )
    assert posted.status_code == 200
    assert posted.json()["status"] == "posted"
    assert posted.json()["posted_at"] is not None

    # проведённую нельзя ни править, ни удалять
    assert api_client.patch(
        f"/api/accounting/money-movements/{mm_id}", json={"amount": 1}
    ).status_code == 409
    assert api_client.delete(f"/api/accounting/money-movements/{mm_id}").status_code == 409

    # назад по статусу тоже нельзя
    assert api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status", json={"to": "approved"}
    ).status_code == 409


def test_cancel_requires_reason(db, api, acc_user):
    client = _client(db)
    api_client = api(acc_user)
    mm_id = _create(api_client, client_id=client.id).json()["id"]

    assert api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status", json={"to": "cancelled"}
    ).status_code == 422

    ok = api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status",
        json={"to": "cancelled", "reason": "ошиблись контрагентом"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "cancelled"
    assert ok.json()["cancel_reason"] == "ошиблись контрагентом"


def test_salary_payout_needs_employee_and_runs_in_three_steps(db, api, acc_user, make_user):
    employee = make_user()
    api_client = api(acc_user)

    assert _create(api_client, subkind="salary_payout").status_code == 422

    mm_id = _create(api_client, subkind="salary_payout", employee_id=employee.id).json()["id"]
    assert api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status", json={"to": "posted"}
    ).status_code == 409  # перепрыгнуть начисление→утверждение→выплату нельзя
    assert api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status", json={"to": "approved"}
    ).status_code == 200
    assert api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status", json={"to": "posted"}
    ).status_code == 200


def test_source_ref_invariants(db, api, acc_user, make_user):
    client = _client(db)
    supply = _supply(db, acc_user)
    employee = make_user()
    api_client = api(acc_user)

    # две привязки сразу
    assert _create(
        api_client, subkind="sale_income", client_id=client.id, supply_id=supply.id
    ).status_code == 422
    # подвид требует конкретный источник
    assert _create(api_client, subkind="supply_payment", client_id=client.id).status_code == 422
    assert _create(api_client, subkind="supply_payment", supply_id=supply.id).status_code == 201
    # подвид без источника не принимает привязку
    assert _create(api_client, subkind="tax", employee_id=employee.id).status_code == 422
    assert _create(api_client, subkind="other_income").status_code == 201
    # несуществующий источник
    assert _create(api_client, subkind="sale_income", client_id=999999).status_code == 422


def test_amount_must_be_positive(db, api, acc_user):
    client = _client(db)
    assert _create(api(acc_user), client_id=client.id, amount=0).status_code == 422
    assert _create(api(acc_user), client_id=client.id, amount=-5).status_code == 422


def test_list_filters(db, api, acc_user, make_user):
    client = _client(db)
    employee = make_user()
    api_client = api(acc_user)

    sale_id = _create(api_client, subkind="sale_income", client_id=client.id).json()["id"]
    _create(api_client, subkind="salary_payout", employee_id=employee.id)
    for to in ("approved", "posted"):
        api_client.post(f"/api/accounting/money-movements/{sale_id}/status", json={"to": to})

    posted_income = api_client.get(
        "/api/accounting/money-movements", params={"status": "posted", "direction": "income"}
    ).json()
    assert [m["id"] for m in posted_income] == [sale_id]

    salary = api_client.get(
        "/api/accounting/money-movements", params={"subkind": "salary_payout"}
    ).json()
    assert len(salary) == 1 and salary[0]["subkind"] == "salary_payout"


def test_requires_module_access(db, api, other_user):
    r = api(other_user).get("/api/accounting/money-movements")
    assert r.status_code == 403


def test_salary_payout_rejects_second_open_movement_for_same_employee(db, api, acc_user, make_user):
    """0023: раздел «Сотрудники» показывает «Начислить» только пока у
    сотрудника нет незакрытой (draft/approved) зарплатной проводки —
    эндпоинт отказывает и на второй draft, и пока первая на approved."""
    employee = make_user()
    api_client = api(acc_user)

    first = _create(api_client, subkind="salary_payout", employee_id=employee.id)
    assert first.status_code == 201

    second = _create(api_client, subkind="salary_payout", employee_id=employee.id)
    assert second.status_code == 409

    api_client.post(
        f"/api/accounting/money-movements/{first.json()['id']}/status", json={"to": "approved"}
    )
    still_open = _create(api_client, subkind="salary_payout", employee_id=employee.id)
    assert still_open.status_code == 409


def test_salary_payout_allowed_again_once_previous_is_posted_or_cancelled(db, api, acc_user, make_user):
    employee = make_user()
    api_client = api(acc_user)

    first = _create(api_client, subkind="salary_payout", employee_id=employee.id).json()
    api_client.post(f"/api/accounting/money-movements/{first['id']}/status", json={"to": "approved"})
    api_client.post(f"/api/accounting/money-movements/{first['id']}/status", json={"to": "posted"})

    reopened = _create(api_client, subkind="salary_payout", employee_id=employee.id)
    assert reopened.status_code == 201

    api_client.post(
        f"/api/accounting/money-movements/{reopened.json()['id']}/status",
        json={"to": "cancelled", "reason": "ошибка ввода"},
    )
    third = _create(api_client, subkind="salary_payout", employee_id=employee.id)
    assert third.status_code == 201


def test_salary_payout_invariant_is_per_employee(db, api, acc_user, make_user):
    a = make_user()
    b = make_user()
    api_client = api(acc_user)

    assert _create(api_client, subkind="salary_payout", employee_id=a.id).status_code == 201
    assert _create(api_client, subkind="salary_payout", employee_id=b.id).status_code == 201


def test_salary_overview_lists_active_employees_with_open_movement(db, api, acc_user, make_user):
    with_open = make_user()
    without_open = make_user()
    api_client = api(acc_user)

    _create(api_client, subkind="salary_payout", employee_id=with_open.id, amount=50000)

    overview = {
        row["employee_id"]: row
        for row in api_client.get("/api/accounting/salary-overview").json()
    }
    assert without_open.id in overview and overview[without_open.id]["open_movement"] is None
    assert with_open.id in overview
    assert overview[with_open.id]["open_movement"]["status"] == "draft"
    assert overview[with_open.id]["open_movement"]["amount"] == 50000


def test_salary_overview_requires_module_access(db, api, other_user):
    r = api(other_user).get("/api/accounting/salary-overview")
    assert r.status_code == 403
