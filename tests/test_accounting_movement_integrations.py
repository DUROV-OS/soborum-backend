"""Сшивка «Движения денег» с источниками — задача 0011-f:

- «доход от продажи» из `clients` на переходе `is_paid`/`balance_paid` в `True`;
- «оплата поставки» из `SupplierOrder` (`0011-d`) через `POST .../pay`;
- задача на согласование при создании любой проводки в `draft`, закрытие при
  уходе из `draft`.

Сам реестр и статусная машина проверены в `test_accounting_money_movement.py`
(`0011-c`), заказы у поставщика — в `test_accounting_supplier_orders.py`
(`0011-d`). Здесь — только сшивка.
"""

import pytest

from app.accounting.models import MoneyMovement, MoneyMovementStatus, MoneySubkind
from app.accounting import service as accounting_service
from app.clients import service as client_service
from app.clients.models import PaymentPlan
from app.clients.schemas import (
    ClientBalancePaymentUpdate,
    ClientCreate,
    ClientDocumentsUpdate,
    ClientPaymentUpdate,
    ClientProjectUpdate,
)
from app.common.module_access import Module
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.warehouse.models import Supplier

PROJECT = ClientProjectUpdate(
    order_type="single", wishes_description="дом у озера", estimated_price=1_000_000,
    house_area=120, layout_notes="две спальни",
)


@pytest.fixture
def acc_user(make_user):
    return make_user(Module.ACCOUNTING, Module.WAREHOUSE)


def _make_client(db, plan=PaymentPlan.FULL_PREPAYMENT, advance_amount=None, final_price=2_000_000):
    client = client_service.create_client(
        db, ClientCreate(full_name="Иван Тест", phone="+70000000000", email="ivan@example.com")
    )
    client_service.transition_stage(db, client)  # LEAD -> DISCUSSION
    client_service.update_project(db, client, PROJECT)
    client_service.transition_stage(db, client)  # DISCUSSION -> APPROVAL
    client.contract_file_id = 1
    client.house_project_file_id = 1
    client_service.update_documents(db, client, ClientDocumentsUpdate(
        final_price=final_price, installation_address="г. Тест, ул. Тест, 1",
        payment_plan=plan, advance_amount=advance_amount,
    ))
    client_service.transition_stage(db, client)  # APPROVAL -> PAYMENT
    db.commit()
    return client


def _open_approval_task(db, mm_id):
    return (
        db.query(Task)
        .filter(Task.link_type == TaskLinkType.MONEY_MOVEMENT_APPROVAL, Task.link_id == mm_id)
        .first()
    )


def _supplier_order(db, api_client, supplier=None):
    supplier = supplier or Supplier(name="ООО Брус", categories=[], contacts=[])
    if supplier.id is None:
        db.add(supplier)
        db.commit()
    resp = api_client.post(
        "/api/accounting/supplier-orders",
        json={
            "supplier_id": supplier.id,
            "items": [{"material": "Брус", "category": None, "quantity": 10, "unit_price": 5000}],
        },
    )
    assert resp.status_code == 201
    return supplier, resp.json()


# --- «доход от продажи» из clients ---


def test_full_prepayment_is_paid_creates_sale_income_draft_with_approval_task(db, acc_user):
    client = _make_client(db, PaymentPlan.FULL_PREPAYMENT, final_price=2_000_000)

    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True), acc_user.id)
    db.commit()

    movements = db.query(MoneyMovement).filter_by(subkind=MoneySubkind.SALE_INCOME).all()
    assert len(movements) == 1
    mm = movements[0]
    assert mm.amount == 2_000_000
    assert mm.client_id == client.id
    assert mm.status is MoneyMovementStatus.DRAFT
    assert mm.initiator_id == acc_user.id

    task = _open_approval_task(db, mm.id)
    assert task is not None and task.status != TaskStatus.DONE


def test_repeat_save_of_already_true_flag_does_not_duplicate_movement(db, acc_user):
    client = _make_client(db, PaymentPlan.FULL_PREPAYMENT)
    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True), acc_user.id)
    db.commit()

    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True), acc_user.id)
    db.commit()

    assert db.query(MoneyMovement).filter_by(subkind=MoneySubkind.SALE_INCOME).count() == 1


def test_advance_then_balance_creates_two_movements_for_advance_and_remainder(db, acc_user):
    client = _make_client(db, PaymentPlan.ADVANCE_THEN_BALANCE, advance_amount=500_000, final_price=2_000_000)

    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True), acc_user.id)
    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT
    db.commit()

    client_service.record_balance_payment(
        db, client, ClientBalancePaymentUpdate(balance_paid=True), acc_user.id
    )
    db.commit()

    movements = (
        db.query(MoneyMovement)
        .filter_by(subkind=MoneySubkind.SALE_INCOME, client_id=client.id)
        .order_by(MoneyMovement.id)
        .all()
    )
    assert [m.amount for m in movements] == [500_000, 1_500_000]


def test_post_payment_plan_no_movement_on_is_paid_but_movement_on_balance_paid(db, acc_user):
    client = _make_client(db, PaymentPlan.POST_PAYMENT, final_price=2_000_000)

    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT, is_paid forced False
    db.commit()
    assert db.query(MoneyMovement).filter_by(subkind=MoneySubkind.SALE_INCOME).count() == 0

    client_service.record_balance_payment(
        db, client, ClientBalancePaymentUpdate(balance_paid=True), acc_user.id
    )
    db.commit()

    movements = db.query(MoneyMovement).filter_by(subkind=MoneySubkind.SALE_INCOME).all()
    assert len(movements) == 1
    assert movements[0].amount == 2_000_000


def test_accounting_error_does_not_break_client_payment_flow(db, acc_user, monkeypatch):
    client = _make_client(db, PaymentPlan.FULL_PREPAYMENT)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(accounting_service, "record_sale_income", _boom)

    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True), acc_user.id)
    db.commit()

    assert client.is_paid is True
    assert db.query(MoneyMovement).filter_by(subkind=MoneySubkind.SALE_INCOME).count() == 0


def test_direct_service_call_without_initiator_skips_movement(db):
    """Вызовы сервиса без `initiator_id` (существующие тесты цикла клиента,
    внутренние сценарии) не должны падать и не создают проводку — некому
    приписать инициатора."""
    client = _make_client(db, PaymentPlan.FULL_PREPAYMENT)
    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True))
    db.commit()
    assert db.query(MoneyMovement).filter_by(subkind=MoneySubkind.SALE_INCOME).count() == 0


# --- задача на согласование: создание/закрытие ---


def test_approval_task_closes_on_status_change_away_from_draft(db, api, acc_user):
    api_client = api(acc_user)
    resp = api_client.post(
        "/api/accounting/money-movements", json={"subkind": "other_income", "amount": 1000}
    )
    mm_id = resp.json()["id"]

    task = _open_approval_task(db, mm_id)
    assert task is not None and task.status != TaskStatus.DONE

    api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "approved"})
    db.refresh(task)
    assert task.status == TaskStatus.DONE


def test_approval_task_closes_on_cancel_from_draft(db, api, acc_user):
    api_client = api(acc_user)
    mm_id = api_client.post(
        "/api/accounting/money-movements", json={"subkind": "other_expense", "amount": 500}
    ).json()["id"]

    api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status",
        json={"to": "cancelled", "reason": "ошиблись"},
    )
    task = _open_approval_task(db, mm_id)
    assert task.status == TaskStatus.DONE


# --- «оплата поставки» из SupplierOrder ---


def test_pay_supplier_order_creates_draft_movement_bound_to_order(db, api, acc_user):
    api_client = api(acc_user)
    supplier, order = _supplier_order(db, api_client)

    resp = api_client.post(f"/api/accounting/supplier-orders/{order['id']}/pay")
    assert resp.status_code == 201
    data = resp.json()
    assert data["subkind"] == "supply_payment"
    assert data["supply_id"] == order["id"]
    assert data["amount"] == order["total_cost"]
    assert data["status"] == "draft"


def test_repeat_pay_on_same_order_conflicts(db, api, acc_user):
    api_client = api(acc_user)
    supplier, order = _supplier_order(db, api_client)

    first = api_client.post(f"/api/accounting/supplier-orders/{order['id']}/pay")
    assert first.status_code == 201

    second = api_client.post(f"/api/accounting/supplier-orders/{order['id']}/pay")
    assert second.status_code == 409


def test_posting_supply_payment_increases_supplier_total_paid_independent_of_delivery_status(db, api, acc_user):
    api_client = api(acc_user)
    supplier, order = _supplier_order(db, api_client)

    mm_id = api_client.post(f"/api/accounting/supplier-orders/{order['id']}/pay").json()["id"]
    api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "approved"})
    posted = api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "posted"})
    assert posted.status_code == 200

    supplier_data = api_client.get(f"/api/warehouse/suppliers/{supplier.id}").json()
    assert supplier_data["total_paid"] == order["total_cost"]
    assert supplier_data["balance"] == 0

    # Оплата не трогает статус физической приёмки заказа.
    order_after = api_client.get(f"/api/accounting/supplier-orders/{order['id']}").json()
    assert order_after["status"] == "ordered"


def test_cancelling_posted_supply_payment_reverts_supplier_total_paid(db, api, acc_user):
    api_client = api(acc_user)
    supplier, order = _supplier_order(db, api_client)

    mm_id = api_client.post(f"/api/accounting/supplier-orders/{order['id']}/pay").json()["id"]
    api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "approved"})
    api_client.post(f"/api/accounting/money-movements/{mm_id}/status", json={"to": "posted"})

    cancel = api_client.post(
        f"/api/accounting/money-movements/{mm_id}/status",
        json={"to": "cancelled", "reason": "оплатили не тому"},
    )
    assert cancel.status_code == 200

    supplier_data = api_client.get(f"/api/warehouse/suppliers/{supplier.id}").json()
    assert supplier_data["total_paid"] == 0
    assert supplier_data["balance"] == order["total_cost"]


def test_pay_on_missing_order_is_404(db, api, acc_user):
    resp = api(acc_user).post("/api/accounting/supplier-orders/999999/pay")
    assert resp.status_code == 404
