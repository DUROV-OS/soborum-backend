"""Импорт прайс-листа поставщика таблицей + разметка колонок (задача 0011-g).

ИИ-путь недетерминированный — основные проверки идут по эвристике (без ключа),
плюс один тест с подменённым `_ai_mapping`.
"""

import io

import pytest
from openpyxl import Workbook

from app.common.module_access import Module
from app.tasks.models import Task, TaskLinkType
from app.warehouse import price_import
from app.warehouse.price_import import ColumnMapping


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    # По умолчанию гоним эвристику; отдельный тест включает ИИ сам.
    monkeypatch.setattr(price_import, "ai_enabled", lambda: False)


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_supplier(client) -> int:
    return client.post("/api/warehouse/suppliers", json={"name": "ООО Тест"}).json()["id"]


def _upload(client, sid: int, content: bytes, filename: str = "price.xlsx"):
    return client.post(
        f"/api/warehouse/suppliers/{sid}/price-items/import",
        files={"file": (filename, content, "application/octet-stream")},
    )


def test_full_table_maps_every_column_no_task(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    sid = _make_supplier(client)
    content = _xlsx(
        [
            ["Наименование", "Категория", "Цена, руб", "Срок поставки, дн"],
            ["Доска 150x50", "брусы/доска", "980", "7"],
            ["Брус 100x100", "брусы/доска", "1 250,50", "10"],
        ]
    )
    res = _upload(client, sid, content)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["imported"] == 2
    assert body["skipped"] == 0
    assert body["ai_used"] is False
    assert body["missing_fields"] == []
    assert body["task_id"] is None
    assert body["column_mapping"]["material"] == "Наименование"
    assert body["column_mapping"]["price"] == "Цена, руб"

    supplier = body["supplier"]
    assert supplier["price_items_count"] == 2
    first = supplier["price_items"][0]
    assert first["material"] == "Доска 150x50"
    assert first["category"] == "брусы/доска"
    assert first["lead_time"] == "7"
    assert first["tiers"] == [{"min_qty": 0, "max_qty": None, "price": 980}]
    assert supplier["price_items"][1]["tiers"][0]["price"] == 1250.5


def test_partial_table_fills_empty_and_creates_backfill_task(api, make_user, db):
    client = api(make_user(Module.WAREHOUSE))
    sid = _make_supplier(client)
    content = _xlsx([["Товар", "Цена"], ["Саморез 4.2x75", "690"], ["Уголок 90x90", "38"]])

    res = _upload(client, sid, content)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["imported"] == 2
    assert sorted(body["missing_fields"]) == ["category", "lead_time"]
    assert body["task_id"] is not None

    task = db.get(Task, body["task_id"])
    assert task.link_type == TaskLinkType.SUPPLIER_PRICE_BACKFILL
    assert task.link_id == sid
    assert "Дозаполнить прайс" in task.title
    assert task.assignees  # сотрудники склада

    item = body["supplier"]["price_items"][0]
    assert item["category"] is None and item["lead_time"] is None


def test_reject_when_price_column_not_recognised(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    sid = _make_supplier(client)
    content = _xlsx([["Наименование", "Артикул"], ["Доска", "ART-1"]])

    res = _upload(client, sid, content)
    assert res.status_code == 400
    assert "колонку с ценой" in res.json()["detail"]
    assert client.get(f"/api/warehouse/suppliers/{sid}").json()["price_items_count"] == 0


def test_quantity_break_columns_become_tiers(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    sid = _make_supplier(client)
    content = _xlsx(
        [
            ["Материал", "Цена от 1", "Цена от 100", "Цена от 500"],
            ["Доска 150x50", "980", "930", "880"],
        ]
    )
    res = _upload(client, sid, content)
    assert res.status_code == 200, res.text
    tiers = res.json()["supplier"]["price_items"][0]["tiers"]
    assert tiers == [
        {"min_qty": 1, "max_qty": 100, "price": 980},
        {"min_qty": 100, "max_qty": 500, "price": 930},
        {"min_qty": 500, "max_qty": None, "price": 880},
    ]


def test_csv_input(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    sid = _make_supplier(client)
    csv_bytes = "Наименование;Цена\nДоска;980\nБрус;1250\n".encode("utf-8")
    res = _upload(client, sid, csv_bytes, filename="price.csv")
    assert res.status_code == 200, res.text
    assert res.json()["imported"] == 2


def test_ai_mapping_path_is_marked(api, make_user, monkeypatch):
    monkeypatch.setattr(price_import, "ai_enabled", lambda: True)
    monkeypatch.setattr(
        price_import,
        "_ai_mapping",
        lambda headers, sample: ColumnMapping(
            material="Позиция", price="Прайс", category=None, lead_time=None, note="категория не найдена"
        ),
    )
    client = api(make_user(Module.WAREHOUSE))
    sid = _make_supplier(client)
    content = _xlsx([["Позиция", "Прайс"], ["Доска", "980"]])

    res = _upload(client, sid, content)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ai_used"] is True
    assert body["imported"] == 1
    assert body["task_id"] is not None
