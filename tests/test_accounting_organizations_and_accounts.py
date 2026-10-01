"""Организации, банковские счета и сводка прихода/расхода (задача 0081-a).

Деньги компании идут через два юрлица; проводка принадлежит ровно одному
счёту, реестр и сводка режутся по счёту/организации. Проверяем: счёт
обязателен, чужие проводки в срез не попадают, сводка считает приход, расход
и сальдо по каждому уровню.
"""

import pytest

from app.accounting.models import BankAccount, Organization
from app.common.module_access import Module


@pytest.fixture
def acc_user(make_user):
    return make_user(Module.ACCOUNTING)


def _accounts(api_client) -> list[dict]:
    return api_client.get("/api/accounting/accounts").json()


def _create(api_client, account_id, **overrides):
    body = {"subkind": "other_income", "amount": 1000, "tax": 0, "account_id": account_id}
    body.update(overrides)
    return api_client.post("/api/accounting/money-movements", json=body)


def _post(api_client, mm_id):
    """Провести проводку: сводка по умолчанию считает только проведённые."""
    api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "approved"})
    return api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "posted"})


def test_seed_creates_two_organizations_each_with_a_default_account(db, api, acc_user):
    orgs = api(acc_user).get("/api/accounting/organizations").json()
    assert [o["short_name"] for o in orgs] == ["ИД Групп", "Технология"]
    for org in orgs:
        assert len(org["accounts"]) == 1
        assert org["accounts"][0]["is_default"] is True
        assert org["accounts"][0]["currency"] == "RUB"


def test_organizations_seed_is_idempotent(db):
    from app.accounting.seed import ensure_organizations_seed

    # conftest уже прогнал сид один раз — повторный не должен плодить дубликаты
    created = ensure_organizations_seed(db)
    assert created == 0
    assert db.query(Organization).count() == 2
    assert db.query(BankAccount).count() == 2


def test_movement_without_account_is_rejected(db, api, acc_user):
    api_client = api(acc_user)
    resp = api_client.post(
        "/api/accounting/money-movements", json={"subkind": "other_income", "amount": 1000}
    )
    assert resp.status_code == 422
    assert "счёт" in resp.json()["detail"].lower()


def test_movement_on_closed_account_is_rejected(db, api, acc_user):
    api_client = api(acc_user)
    account = _accounts(api_client)[0]
    api_client.patch(f"/api/accounting/accounts/{account['id']}", json={"is_active": False})

    resp = _create(api_client, account["id"])
    assert resp.status_code == 422
    # закрытый счёт пропадает из списка действующих
    assert account["id"] not in [a["id"] for a in _accounts(api_client)]


def test_register_shows_only_movements_of_the_selected_account(db, api, acc_user):
    api_client = api(acc_user)
    first, second = _accounts(api_client)

    mine = _create(api_client, first["id"], amount=1000).json()["id"]
    theirs = _create(api_client, second["id"], amount=2000).json()["id"]

    by_account = api_client.get(
        "/api/accounting/money-movements", params={"account_id": first["id"]}
    ).json()
    assert [m["id"] for m in by_account] == [mine]
    assert by_account[0]["account_name"] == first["name"]
    assert by_account[0]["organization_id"] == first["organization_id"]

    by_org = api_client.get(
        "/api/accounting/money-movements", params={"organization_id": second["organization_id"]}
    ).json()
    assert [m["id"] for m in by_org] == [theirs]


def test_summary_counts_income_expense_and_balance_per_account_and_total(db, api, acc_user):
    api_client = api(acc_user)
    first, second = _accounts(api_client)

    _post(api_client, _create(api_client, first["id"], subkind="other_income", amount=1500).json()["id"])
    _post(api_client, _create(api_client, first["id"], subkind="other_expense", amount=400).json()["id"])
    _post(api_client, _create(api_client, second["id"], subkind="other_income", amount=700).json()["id"])
    # черновик в сводку не идёт — деньги ещё не прошли
    _create(api_client, second["id"], subkind="other_income", amount=99999)

    summary = api_client.get("/api/accounting/money-summary").json()

    first_org = summary["organizations"][0]
    assert first_org["income"] == 1500
    assert first_org["expense"] == 400
    assert first_org["balance"] == 1100
    assert first_org["accounts"][0]["account_id"] == first["id"]
    assert first_org["accounts"][0]["balance"] == 1100

    second_org = summary["organizations"][1]
    assert second_org["income"] == 700
    assert second_org["balance"] == 700

    assert summary["total"]["income"] == 2200
    assert summary["total"]["expense"] == 400
    assert summary["total"]["balance"] == 1800
    assert summary["total"]["count"] == 3


def test_summary_can_be_narrowed_to_one_organization(db, api, acc_user):
    api_client = api(acc_user)
    first, second = _accounts(api_client)
    _post(api_client, _create(api_client, first["id"], amount=1000).json()["id"])
    _post(api_client, _create(api_client, second["id"], amount=5000).json()["id"])

    summary = api_client.get(
        "/api/accounting/money-summary", params={"organization_id": second["organization_id"]}
    ).json()
    assert len(summary["organizations"]) == 1
    assert summary["total"]["income"] == 5000


def test_new_account_of_an_organization_is_not_default_and_can_take_over(db, api, acc_user):
    api_client = api(acc_user)
    org_id = _accounts(api_client)[0]["organization_id"]

    created = api_client.post(
        "/api/accounting/accounts",
        json={"organization_id": org_id, "name": "Счёт в Т-Банке", "bank_name": "Т-Банк"},
    )
    assert created.status_code == 201
    second_account = created.json()
    assert second_account["is_default"] is False

    api_client.patch(f"/api/accounting/accounts/{second_account['id']}", json={"is_default": True})
    org_accounts = [
        a for a in _accounts(api_client) if a["organization_id"] == org_id
    ]
    defaults = [a["id"] for a in org_accounts if a["is_default"]]
    assert defaults == [second_account["id"]]


def test_account_of_unknown_organization_is_404(db, api, acc_user):
    resp = api(acc_user).post(
        "/api/accounting/accounts", json={"organization_id": 999, "name": "Счёт"}
    )
    assert resp.status_code == 404
