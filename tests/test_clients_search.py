"""Поиск клиентов по фамилии и телефону (0079-f):

- находит по части ФИО без учёта регистра;
- находит по телефону в любой записи: `+7 900 …`, `8900…`, последние цифры;
- ищет по всем стадиям сразу;
- пустой запрос ничего не фильтрует, заведомо чужая строка не находит ничего;
- по почте не ищет — заказчик просил фамилию и телефон.
"""

from app.clients import service as client_service
from app.clients.models import Client, ClientStage
from app.clients.schemas import ClientCreate
from app.common.module_access import Module


def _make(db, name, phone, stage=ClientStage.LEAD):
    client = client_service.create_client(
        db, ClientCreate(full_name=name, phone=phone, email="client@example.com")
    )
    client.stage = stage
    db.flush()
    return client


def _found(db, search):
    found = client_service.search_clients(db.query(Client).all(), search)
    return sorted(c.full_name for c in found)


def test_finds_by_any_part_of_the_name(db):
    _make(db, "Иванов Иван Иванович", "+7 900 111-11-11")
    _make(db, "Петров Пётр Петрович", "+7 900 222-22-22")
    db.commit()

    assert _found(db, "иванов") == ["Иванов Иван Иванович"]
    assert _found(db, "ПЕТР") == ["Петров Пётр Петрович"]


def test_finds_by_phone_in_any_format(db):
    _make(db, "Иванов Иван Иванович", "+7 900 123-45-67")
    _make(db, "Петров Пётр Петрович", "8 (901) 000-00-00")
    db.commit()

    for query in ("+7 900 123-45-67", "89001234567", "79001234567", "9001234567", "1234567"):
        assert _found(db, query) == ["Иванов Иван Иванович"], query

    assert _found(db, "89010000000") == ["Петров Пётр Петрович"]


def test_search_spans_all_stages(db):
    _make(db, "Сидоров Лид", "+7 900 333-33-33", stage=ClientStage.LEAD)
    _make(db, "Сидоров Принятый", "+7 900 444-44-44", stage=ClientStage.COMPLETED)
    db.commit()

    assert _found(db, "сидоров") == ["Сидоров Лид", "Сидоров Принятый"]


def test_empty_query_does_not_filter_and_unknown_finds_nothing(db):
    _make(db, "Иванов Иван Иванович", "+7 900 123-45-67")
    db.commit()

    assert _found(db, "") == ["Иванов Иван Иванович"]
    assert _found(db, None) == ["Иванов Иван Иванович"]
    assert _found(db, "Несуществующий") == []
    # по почте не ищем
    assert _found(db, "client@example.com") == []


def test_search_query_param(api, make_user, db):
    _make(db, "Иванов Иван Иванович", "+7 900 123-45-67")
    _make(db, "Петров Пётр Петрович", "+7 901 000-00-00")
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    by_name = worker.get("/api/clients/?search=иванов")
    assert by_name.status_code == 200, by_name.text
    assert [c["full_name"] for c in by_name.json()] == ["Иванов Иван Иванович"]

    by_phone = worker.get("/api/clients/?search=89001234567")
    assert [c["full_name"] for c in by_phone.json()] == ["Иванов Иван Иванович"]

    assert len(worker.get("/api/clients/").json()) == 2
