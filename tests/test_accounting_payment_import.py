"""Импорт платежей в «Бухгалтерию» таблицей (app.accounting.payment_import) — 0011-k.

По образцу импорта прайс-листа: критичных колонок нет → отказ; строки без
суммы пропускаются; вид из файла или «предварительный» по знаку; контрагент
по уверенному совпадению имени привязывается к клиенту; ИИ-вид и задача на
дозаполнение — отдельными шагами.
"""

import io

import pytest
from openpyxl import Workbook

from app.accounting.models import MoneyMovement
from app.clients import service as client_service
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.tasks.models import Task, TaskLinkType

HEAD = "Дата,Контрагент,Назначение платежа,Сумма,НДС,№ документа,Вид\n"


@pytest.fixture
def acc_user(make_user):
    return make_user(Module.ACCOUNTING, admin=True)


def _csv(*rows: str) -> bytes:
    return (HEAD + "".join(r + "\n" for r in rows)).encode("utf-8")


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Дата", "Контрагент", "Назначение платежа", "Сумма", "НДС", "№ документа", "Вид"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _import(api_client, content: bytes, name="pay.csv", mime="text/csv"):
    return api_client.post(
        "/api/accounting/money-movements/import", files={"file": (name, content, mime)}
    )


def test_import_creates_drafts_with_sign_direction(db, api, acc_user):
    body = _csv(
        "01.09.2026,ООО Ромашка,оплата по счёту,150000,25000,125,",
        "03.09.2026,ИФНС,НДС за квартал,-274000,0,126,налоги и сборы",
    )
    r = _import(api(acc_user), body)
    assert r.status_code == 200
    data = r.json()
    assert data["imported"] == 2 and data["skipped"] == 0
    assert data["column_mapping"]["amount"] == "Сумма"

    rows = {m.subkind.value: m for m in db.query(MoneyMovement).all()}
    assert rows["tax"].direction.value == "expense" and rows["tax"].amount == 274000
    # без вида в файле и без совпадения клиента — «прочий доход» по знаку +
    assert "other_income" in rows
    assert all(m.status.value == "draft" for m in rows.values())
    assert rows["tax"].doc_date is not None and rows["tax"].external_number == "126"


def test_counterparty_matches_client_and_upgrades_subkind(db, api, acc_user):
    client_service.create_client(
        db, ClientCreate(full_name="ООО «Ромашка»", phone="+70000000000", email="r@e.co")
    )
    r = _import(api(acc_user), _csv("01.09.2026,ООО Ромашка,аванс,150000,25000,125,"))
    assert r.status_code == 200
    mm = db.query(MoneyMovement).one()
    assert mm.subkind.value == "sale_income" and mm.source_kind.value == "client"
    assert mm.client_id is not None and mm.comment is None


def test_unmatched_counterparty_goes_to_comment(db, api, acc_user):
    r = _import(api(acc_user), _csv("01.09.2026,Стороннее ООО,услуги,-3200,0,127,"))
    data = r.json()
    assert data["unmatched_source"] == 1
    mm = db.query(MoneyMovement).one()
    assert mm.source_kind.value == "none"
    assert "Стороннее ООО" in (mm.comment or "")
    assert mm.subkind.value == "other_expense"


def test_rows_without_amount_are_skipped(db, api, acc_user):
    body = _csv(
        "01.09.2026,A,платёж,100000,0,1,",
        "02.09.2026,B,без суммы,,0,2,",
        "03.09.2026,C,мусор,---,0,3,",
    )
    data = _import(api(acc_user), body).json()
    assert data["imported"] == 1 and data["skipped"] == 2


def test_xlsx_import_matches_csv(db, api, acc_user):
    content = _xlsx([["01.09.2026", "ООО Тест", "оплата", 90000, 15000, "9", "аренда"]])
    r = _import(api(acc_user), content, name="pay.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert r.status_code == 200 and r.json()["imported"] == 1
    assert db.query(MoneyMovement).one().subkind.value == "rent"


@pytest.mark.parametrize(
    "head",
    [
        "Дата,Контрагент,Назначение,НДС,№ документа\n",  # нет суммы
        "Контрагент,Назначение,Сумма,НДС,№ документа\n",  # нет даты
        "Дата,Назначение,Сумма,НДС,№ документа\n",  # нет контрагента
        "Дата,Контрагент,Назначение,Сумма,№ документа\n",  # нет НДС
        "Дата,Контрагент,Назначение,Сумма,НДС\n",  # нет номера документа
    ],
)
def test_missing_critical_column_is_rejected(db, api, acc_user, head):
    r = _import(api(acc_user), (head + "x,y,z,1,2,3\n").encode("utf-8"))
    assert r.status_code == 400
    assert db.query(MoneyMovement).count() == 0


def test_ai_fill_subkind_without_key_is_noop(db, api, acc_user):
    ids = _import(api(acc_user), _csv("01.09.2026,X ООО,оплата,-5000,0,1,")).json()["created_ids"]
    r = api(acc_user).post(
        "/api/accounting/money-movements/import/ai-fill-subkind", json={"movement_ids": ids}
    )
    assert r.status_code == 200 and r.json() == {"updated": 0, "skipped": 1}


def test_backfill_task_created_on_demand(db, api, acc_user):
    ids = _import(api(acc_user), _csv("01.09.2026,X ООО,оплата,-5000,0,1,")).json()["created_ids"]
    r = api(acc_user).post(
        "/api/accounting/money-movements/import/backfill-task",
        json={"movement_ids": ids, "missing_fields": ["subkind", "source"]},
    )
    assert r.status_code == 200
    task = db.get(Task, r.json()["task_id"])
    assert task.link_type is TaskLinkType.MONEY_MOVEMENT_BACKFILL
    assert task.link_meta["movement_ids"] == ids


def test_template_endpoint_returns_xlsx(db, api, acc_user):
    r = api(acc_user).get("/api/accounting/money-movements/import/template")
    assert r.status_code == 200
    assert r.content[:2] == b"PK" and "attachment" in r.headers["content-disposition"]
