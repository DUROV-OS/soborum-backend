"""Марина называет стадии клиента так же, как интерфейс (0086).

После 0079 стадии переименовали, но системный промпт описывал прежний путь
(«лид → обсуждение → согласование → оплата → постоплата»), а инструменты
отдавали модели только слаг — и Марина говорила человеку «клиент на стадии
согласования». Тесты держат оба места на одном словаре подписей.
"""

from app.ai.models import ChatDomain
from app.ai.prompts import SYSTEM_PROMPTS
from app.ai.tools import _list_clients, _serialize_client
from app.clients import service as client_service
from app.clients.models import MANUAL_TRANSITION_STAGES, STAGE_LABELS, ClientStage
from app.clients.schemas import ClientCreate

# Названия, которых в интерфейсе нет с 0079: ни одно не должно встречаться в
# промпте как название стадии.
RETIRED_STAGE_NAMES = ("согласование", "согласовани", "постоплат")


def _client(db, name="Иван Тест"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email="ivan@example.com")
    )


def test_clients_prompt_lists_every_actual_stage():
    prompt = SYSTEM_PROMPTS[ChatDomain.CLIENTS]
    for label in STAGE_LABELS.values():
        assert label in prompt, label


def test_clients_prompt_has_no_retired_stage_names():
    prompt = SYSTEM_PROMPTS[ChatDomain.CLIENTS].lower()
    for retired in RETIRED_STAGE_NAMES:
        assert retired not in prompt, retired


def test_clients_prompt_warns_that_last_stages_move_on_their_own():
    prompt = SYSTEM_PROMPTS[ChatDomain.CLIENTS]
    for stage, label in STAGE_LABELS.items():
        if stage not in MANUAL_TRANSITION_STAGES:
            assert label in prompt, label
    assert "Монтаж" in prompt


def test_get_client_gives_model_both_slug_and_label(db):
    client = _client(db)
    client.stage = ClientStage.APPROVAL
    db.flush()

    data = _serialize_client(client)

    assert data["stage"] == "approval"
    assert data["stage_label"] == "Ипотека/Одобрение в банке"


def test_list_clients_gives_model_both_slug_and_label(db, make_user):
    client = _client(db)
    client.stage = ClientStage.POSTPAYMENT
    db.flush()
    db.commit()

    rows = _list_clients(db, make_user())["clients"]

    row = next(r for r in rows if r["id"] == client.id)
    assert row["stage"] == "postpayment"
    assert row["stage_label"] == "Дом в производстве"
