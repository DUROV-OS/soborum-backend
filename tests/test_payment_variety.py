"""Формат расчёта клиента (app.clients.models.PaymentPlan) и то, как он
сдвигает моменты переходов в цикле:

- полная предоплата  — вся сумма на «оплате», до старта производства;
- аванс + остаток    — аванс на «оплате», остаток до завершения цикла;
- оплата после получения — на «оплате» деньги не нужны, вся сумма до завершения.
"""

import pytest
from fastapi import HTTPException

from app.clients import service as client_service
from app.clients.models import Client, ClientStage, PaymentPlan
from app.clients.schemas import (
    ClientBalancePaymentUpdate,
    ClientCreate,
    ClientDocumentsUpdate,
    ClientPaymentUpdate,
    ClientProjectUpdate,
)
from app.cycle.models import CycleStatus
from app.installation import service as installation_service
from app.installation.models import InstallationStage
from app.tasks import sync as task_sync
from app.tasks.models import Task, TaskLinkType, TaskStatus

PROJECT = ClientProjectUpdate(
    order_type="single", wishes_description="дом у озера", estimated_price=1_000_000,
    house_area=120, layout_notes="две спальни",
)


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


def _to_postpayment(db, client, is_paid=None):
    if is_paid is not None:
        client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=is_paid))
    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT
    db.commit()


def _drive_installation_to_followup(db, cycle_id):
    inst = installation_service.start_installation(db, cycle_id)
    installation_service.transition_stage(db, inst)  # DELIVERY -> INSTALLATION
    installation_service.transition_stage(db, inst)  # INSTALLATION -> FOLLOWUP
    assert inst.stage == InstallationStage.FOLLOWUP
    db.commit()
    return inst


def test_existing_clients_default_to_full_prepayment(db):
    client = client_service.create_client(
        db, ClientCreate(full_name="Старый", phone="+7", email="old@example.com")
    )
    db.commit()
    assert client.payment_plan == PaymentPlan.FULL_PREPAYMENT


def test_full_prepayment_requires_full_payment_and_settles_balance(db):
    client = _make_client(db, PaymentPlan.FULL_PREPAYMENT)

    with pytest.raises(HTTPException) as err:
        client_service.transition_stage(db, client)  # без оплаты — нельзя
    assert "полной предоплаты" in err.value.detail

    _to_postpayment(db, client, is_paid=True)
    assert client.stage == ClientStage.POSTPAYMENT
    assert client.cycle.status == CycleStatus.PRODUCTION
    # Остаток «после получения» закрыт полной предоплатой — цикл можно завершить,
    # и отдельная задача на приём остатка не заводится.
    assert client.balance_paid is True
    assert db.query(Task).filter_by(
        link_type=TaskLinkType.CLIENT_BALANCE_PAYMENT, link_id=client.id
    ).count() == 0

    inst = _drive_installation_to_followup(db, client.cycle_id)
    installation_service.complete_installation(db, inst)
    assert client.cycle.status == CycleStatus.COMPLETED


def test_advance_plan_needs_advance_amount_before_payment_stage(db):
    client = client_service.create_client(
        db, ClientCreate(full_name="Аванс", phone="+7", email="a@example.com")
    )
    client_service.transition_stage(db, client)
    client_service.update_project(db, client, PROJECT)
    client_service.transition_stage(db, client)
    client.contract_file_id = 1
    client.house_project_file_id = 1
    client_service.update_documents(db, client, ClientDocumentsUpdate(
        final_price=2_000_000, installation_address="адрес",
        payment_plan=PaymentPlan.ADVANCE_THEN_BALANCE,
    ))
    with pytest.raises(HTTPException) as err:
        client_service.transition_stage(db, client)  # APPROVAL -> PAYMENT без аванса
    assert "сумму аванса" in err.value.detail


def test_advance_plan_starts_production_on_advance_then_gates_completion_on_balance(db):
    client = _make_client(db, PaymentPlan.ADVANCE_THEN_BALANCE, advance_amount=500_000)

    with pytest.raises(HTTPException) as err:
        client_service.transition_stage(db, client)  # аванс не подтверждён
    assert "аванса" in err.value.detail

    _to_postpayment(db, client, is_paid=True)  # аванс получен -> производство
    assert client.cycle.status == CycleStatus.PRODUCTION
    assert client.balance_paid is not True
    balance_task = db.query(Task).filter_by(
        link_type=TaskLinkType.CLIENT_BALANCE_PAYMENT, link_id=client.id
    ).one()

    inst = _drive_installation_to_followup(db, client.cycle_id)
    with pytest.raises(HTTPException) as err:
        installation_service.complete_installation(db, inst)
    assert "оплату после получения" in err.value.detail

    # Закрытие связанной задачи фиксирует приём остатка (sync-хендлер).
    balance_task.status = TaskStatus.DONE
    task_sync.handle_task_closed(db, balance_task)
    assert client.balance_paid is True
    assert client.balance_paid_at is not None

    installation_service.complete_installation(db, inst)
    assert client.cycle.status == CycleStatus.COMPLETED


def test_post_payment_plan_starts_production_without_any_payment(db):
    client = _make_client(db, PaymentPlan.POST_PAYMENT)

    _to_postpayment(db, client, is_paid=None)  # денег на «оплате» не требуется
    assert client.stage == ClientStage.POSTPAYMENT
    assert client.cycle.status == CycleStatus.PRODUCTION
    assert client.is_paid is False
    assert client.payment_locked_at is not None
    assert db.query(Task).filter_by(
        link_type=TaskLinkType.CLIENT_BALANCE_PAYMENT, link_id=client.id
    ).count() == 1

    inst = _drive_installation_to_followup(db, client.cycle_id)
    with pytest.raises(HTTPException):
        installation_service.complete_installation(db, inst)

    client_service.record_balance_payment(db, client, ClientBalancePaymentUpdate(balance_paid=True))
    installation_service.complete_installation(db, inst)
    assert client.cycle.status == CycleStatus.COMPLETED


def test_balance_payment_rejected_for_prepaid_and_before_postpayment(db):
    prepaid = _make_client(db, PaymentPlan.FULL_PREPAYMENT)
    _to_postpayment(db, prepaid, is_paid=True)
    with pytest.raises(HTTPException) as err:
        client_service.record_balance_payment(
            db, prepaid, ClientBalancePaymentUpdate(balance_paid=True)
        )
    assert "полная предоплата" in err.value.detail

    early = _make_client(db, PaymentPlan.POST_PAYMENT)  # ещё на стадии PAYMENT
    assert early.stage == ClientStage.PAYMENT
    with pytest.raises(HTTPException) as err:
        client_service.record_balance_payment(
            db, early, ClientBalancePaymentUpdate(balance_paid=True)
        )
    assert "постоплата" in err.value.detail


def test_balance_payment_endpoint(db, api, make_user):
    from app.common.module_access import Module

    client = _make_client(db, PaymentPlan.POST_PAYMENT)
    _to_postpayment(db, client, is_paid=None)

    response = api(make_user(Module.CLIENTS)).patch(
        f"/api/clients/{client.id}/balance-payment", json={"balance_paid": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["balance_paid"] is True
    assert body["payment_plan"] == "post_payment"
