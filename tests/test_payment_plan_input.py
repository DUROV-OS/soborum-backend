"""Регрессия: инструмент Марины update_client_documents отваливался 422-м,
если человек называл формат расчёта по-русски («Полная предоплата»), а не
enum-значением («full_prepayment»). Тул теперь принимает оба варианта и
понятно отказывает на мусоре (0018)."""

import pytest
from fastapi import HTTPException

from app.ai.tools import TOOLS
from app.clients.models import PaymentPlan, parse_payment_plan
from app.clients.schemas import ClientCreate
from app.clients import service as client_service
from app.common.module_access import Module


def _make_client(db, name="Клиент"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


@pytest.mark.parametrize("value,expected", [
    ("full_prepayment", PaymentPlan.FULL_PREPAYMENT),
    ("advance_then_balance", PaymentPlan.ADVANCE_THEN_BALANCE),
    ("post_payment", PaymentPlan.POST_PAYMENT),
    ("Полная предоплата", PaymentPlan.FULL_PREPAYMENT),
    ("полная предоплата", PaymentPlan.FULL_PREPAYMENT),
    ("Аванс + оплата после получения", PaymentPlan.ADVANCE_THEN_BALANCE),
    ("Аванс и оплата после получения", PaymentPlan.ADVANCE_THEN_BALANCE),
    ("Оплата после получения", PaymentPlan.POST_PAYMENT),
])
def test_parse_payment_plan_accepts_enum_and_russian_labels(value, expected):
    assert parse_payment_plan(value) == expected


def test_parse_payment_plan_rejects_garbage_with_allowed_values_listed():
    with pytest.raises(ValueError) as excinfo:
        parse_payment_plan("Оплата криптовалютой")
    message = str(excinfo.value)
    assert "full_prepayment" in message and "advance_then_balance" in message and "post_payment" in message


def test_update_client_documents_tool_accepts_russian_label(db, make_user):
    user = make_user(Module.CLIENTS, admin=True)
    client = _make_client(db)

    result = TOOLS["update_client_documents"].handler(
        db, user, client_id=client.id, payment_plan="Полная предоплата",
    )

    assert result["payment_plan"] == "full_prepayment"


def test_update_client_documents_tool_accepts_enum_value(db, make_user):
    user = make_user(Module.CLIENTS, admin=True)
    client = _make_client(db)

    result = TOOLS["update_client_documents"].handler(
        db, user, client_id=client.id, payment_plan="post_payment",
    )

    assert result["payment_plan"] == "post_payment"


def test_update_client_documents_tool_rejects_unknown_label_clearly(db, make_user):
    user = make_user(Module.CLIENTS, admin=True)
    client = _make_client(db)

    with pytest.raises(HTTPException) as excinfo:
        TOOLS["update_client_documents"].handler(
            db, user, client_id=client.id, payment_plan="Оплата криптовалютой",
        )

    assert excinfo.value.status_code == 422
    assert "full_prepayment" in excinfo.value.detail
