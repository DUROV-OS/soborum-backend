"""navigate_to (0051-d) — единственный инструмент Jarvis, который реально
переключает экран: резолвит клиента/раздел в путь фронта, а при неоднозначном
или отсутствующем совпадении отдаёт это как результат (candidates/reason),
а не выдумывает id — угадывать карточку должен фронт после подтверждения
пользователем, а не сам инструмент."""

from app.ai.models import ChatDomain
from app.ai.tools import DOMAIN_TOOLS, TOOLS
from app.clients import service as client_service
from app.clients.schemas import ClientCreate

_navigate_to = TOOLS["navigate_to"].handler


def _make_client(db, name):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name.replace(' ', '.')}@example.com")
    )


def test_navigate_to_finds_client_by_unique_name(db, make_user):
    user = make_user()
    client = _make_client(db, "Иванов Иван")
    db.commit()

    result = _navigate_to(db, user, entity_type="client", query="Иванов")

    assert result == {"found": True, "path": f"/clients/{client.id}", "label": "Иванов Иван"}


def test_navigate_to_does_not_guess_ambiguous_client(db, make_user):
    user = make_user()
    _make_client(db, "Иванов Иван")
    _make_client(db, "Иванов Пётр")
    db.commit()

    result = _navigate_to(db, user, entity_type="client", query="Иванов")

    assert result["found"] is False
    assert len(result["candidates"]) == 2


def test_navigate_to_does_not_invent_id_for_unknown_client(db, make_user):
    user = make_user()

    result = _navigate_to(db, user, entity_type="client", query="Несуществующий")

    assert result == {"found": False, "reason": "Клиент «Несуществующий» не найден"}


def test_navigate_to_finds_section_by_label(db, make_user):
    user = make_user()

    result = _navigate_to(db, user, entity_type="section", query="склад")

    assert result == {"found": True, "path": "/warehouse", "label": "Склад"}


def test_navigate_to_reports_unknown_section_instead_of_guessing(db, make_user):
    user = make_user()

    result = _navigate_to(db, user, entity_type="section", query="космос")

    assert result == {"found": False, "reason": "Раздел «космос» не найден"}


def test_navigate_to_is_read_only_and_available_in_general_domain():
    assert TOOLS["navigate_to"].read_only is True
    assert "navigate_to" in DOMAIN_TOOLS[ChatDomain.GENERAL]
