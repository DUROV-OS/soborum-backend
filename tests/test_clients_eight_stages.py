"""Восемь стадий пути клиента (0079-a):

- стадии до «Ипотеки/Одобрения в банке» проходятся без требований, порядок
  колонок соответствует CLIENT_STAGE_ORDER;
- «Дом в производстве» и дальше руками не переводятся — API отказывает;
- монтаж, дойдя до проработки, сам переводит клиента в «Приёмку», а
  завершение монтажа — в «Успешно реализовано»;
- на автоматических стадиях задача «перевести на следующую стадию» не висит;
- повторный автоперевод назад клиента не откатывает.
"""

import pytest
from fastapi import HTTPException

from app.clients import service as client_service
from app.clients.models import CLIENT_STAGE_ORDER, ClientStage
from app.clients.schemas import ClientCreate, ClientDocumentsUpdate, ClientPaymentUpdate
from app.cycle.models import CycleStatus
from app.installation import service as installation_service
from app.installation.models import InstallationStage
from app.tasks.models import Task, TaskLinkType, TaskStatus


def _open_stage_tasks(db, client_id):
    return (
        db.query(Task)
        .filter(
            Task.link_type == TaskLinkType.CLIENT_STAGE,
            Task.link_id == client_id,
            Task.status != TaskStatus.DONE,
        )
        .all()
    )


def _client_in_production(db):
    """Клиент, доведённый до «Дома в производстве» полной предоплатой."""
    client = client_service.create_client(
        db, ClientCreate(full_name="Иван Тест", phone="+70000000000", email="ivan@example.com")
    )
    for _ in range(3):  # LEAD -> DISCUSSION -> SITE_VISIT -> APPROVAL
        client_service.transition_stage(db, client)
    client.contract_file_id = 1
    client.contract_appendix_file_id = 1
    client.ar_file_id = 1
    client.kr_file_id = 1
    client_service.update_documents(db, client, ClientDocumentsUpdate(
        order_type="single", final_price=2_000_000, installation_address="г. Тест, ул. Тест, 1",
    ))
    client_service.transition_stage(db, client)  # APPROVAL -> PAYMENT
    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True))
    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT
    db.commit()
    assert client.stage == ClientStage.POSTPAYMENT
    return client


def test_stage_order_is_eight_columns():
    assert [s.value for s in CLIENT_STAGE_ORDER] == [
        "lead",
        "discussion",
        "site_visit",
        "approval",
        "payment",
        "postpayment",
        "acceptance",
        "completed",
    ]


def test_stages_up_to_approval_need_nothing(db):
    client = client_service.create_client(
        db, ClientCreate(full_name="Лид", phone="+7", email="lead@example.com")
    )
    for expected in (ClientStage.DISCUSSION, ClientStage.SITE_VISIT, ClientStage.APPROVAL):
        client_service.transition_stage(db, client)
        assert client.stage == expected


def test_production_stage_cannot_be_advanced_by_hand(db):
    client = _client_in_production(db)
    with pytest.raises(HTTPException) as err:
        client_service.transition_stage(db, client)
    assert "автоматически" in err.value.detail
    assert client.stage == ClientStage.POSTPAYMENT
    # задачи «перевести на следующую стадию» на такой стадии не заводится
    assert _open_stage_tasks(db, client.id) == []


def test_installation_moves_client_to_acceptance_then_completed(db):
    client = _client_in_production(db)

    inst = installation_service.start_installation(db, client.cycle_id)
    installation_service.transition_stage(db, inst)  # DELIVERY -> INSTALLATION
    assert client.stage == ClientStage.POSTPAYMENT

    installation_service.transition_stage(db, inst)  # INSTALLATION -> FOLLOWUP
    assert inst.stage == InstallationStage.FOLLOWUP
    assert client.stage == ClientStage.ACCEPTANCE

    installation_service.complete_installation(db, inst)
    assert inst.cycle.status == CycleStatus.COMPLETED
    assert client.stage == ClientStage.COMPLETED
    db.commit()


def test_automatic_advance_never_moves_client_back(db):
    client = _client_in_production(db)
    client_service.advance_stage_automatically(db, client, ClientStage.COMPLETED)
    assert client.stage == ClientStage.COMPLETED

    client_service.advance_stage_automatically(db, client, ClientStage.ACCEPTANCE)
    assert client.stage == ClientStage.COMPLETED
