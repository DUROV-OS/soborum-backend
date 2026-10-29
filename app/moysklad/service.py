"""Department-scoped access to imported records; no live MoySklad dependency."""

from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import func, Integer
from app.common.module_access import Module
from app.core.config import settings
from app.moysklad.models import SourceRecord, ImportRun
from app.users.models import UserRole

TITLES = {
    "product": "Товары",
    "variant": "Модификации",
    "service": "Услуги",
    "bundle": "Комплекты",
    "productfolder": "Группы товаров",
    "counterparty": "Контрагенты",
    "organization": "Юрлица",
    "store": "Склады",
    "employee": "Сотрудники МоегоСклада",
    "customerorder": "Заказы покупателей",
    "purchaseorder": "Заказы поставщикам",
    "demand": "Отгрузки",
    "supply": "Приёмки",
    "invoicein": "Счета поставщиков",
    "invoiceout": "Счета покупателям",
    "paymentin": "Входящие платежи",
    "paymentout": "Исходящие платежи",
    "cashin": "Приходные ордера",
    "cashout": "Расходные ордера",
    "processingplan": "Техкарты",
    "processingplanfolder": "Группы техкарт",
    "processing": "Техоперации",
    "processingorder": "Заказы на производство",
    "productiontask": "Производственные задания",
    "productionstagecompletion": "Выполненные этапы",
    "move": "Перемещения",
    "enter": "Оприходования",
    "loss": "Списания",
    "inventory": "Инвентаризации",
    "project": "Проекты",
    "expenseitem": "Статьи расходов",
    "currency": "Валюты",
    "uom": "Единицы измерения",
    "report/stock/all": "Остатки — общий снимок",
    "report/stock/bystore": "Остатки по складам",
    "report/counterparty": "Взаиморасчёты",
    "report/profit/byproduct": "Прибыль по товарам",
    "report/profit/bycounterparty": "Прибыль по контрагентам",
    "facturein": "Счета-фактуры полученные",
    "factureout": "Счета-фактуры выданные",
    "contract": "Договоры",
    "task": "Задачи МоегоСклада",
    "country": "Страны",
    "group": "Группы доступа МоегоСклада",
    "retailstore": "Розничные точки",
    "metadata": "Статусы и дополнительные поля",
    "processingprocess": "Техпроцессы",
    "processingstage": "Этапы производства",
}
WAREHOUSE = {
    "product",
    "variant",
    "service",
    "bundle",
    "productfolder",
    "store",
    "supply",
    "move",
    "enter",
    "loss",
    "inventory",
    "uom",
    "report/stock/all",
    "report/stock/bystore",
}
PRODUCTION = {
    "processingplan",
    "processingplanfolder",
    "processing",
    "processingorder",
    "productiontask",
    "productionstagecompletion",
    "processingprocess",
    "processingstage",
    "project",
}
CLIENTS = {"customerorder", "demand", "invoiceout", "salesreturn"}
FINANCE = {
    "counterparty",
    "organization",
    "expenseitem",
    "currency",
    "purchaseorder",
    "invoicein",
    "paymentin",
    "paymentout",
    "cashin",
    "cashout",
    "facturein",
    "factureout",
    "contract",
    "report/counterparty",
    "report/profit/byproduct",
    "report/profit/bycounterparty",
    "counterpartyadjustment",
}


def allowed(user, typ):
    if typ.startswith("amocrm/"):
        return False
    if user.role == UserRole.ADMIN:
        return True
    if typ.startswith("report/stock/store/"):
        return user.has_access(Module.WAREHOUSE)
    if typ in WAREHOUSE:
        return user.has_access(Module.WAREHOUSE)
    if typ in PRODUCTION:
        return user.has_access(Module.PRODUCTION)
    if typ in CLIENTS:
        return user.has_access(Module.CLIENTS)
    if typ in FINANCE:
        return user.has_access(Module.ACCOUNTING)
    return False


def catalog(db, user):
    result = []
    stores = {r.source_id: r.title for r in db.query(SourceRecord).filter_by(entity_type="store")}
    for typ, count, archived in db.query(
        SourceRecord.entity_type, func.count(SourceRecord.id), func.sum(SourceRecord.archived.cast(Integer))
    ).group_by(SourceRecord.entity_type):
        if allowed(user, typ):
            result.append(
                {
                    "type": typ,
                    "title": (
                        ("Себестоимость · " + stores.get(typ.rsplit("/", 1)[-1], ""))
                        if typ.startswith("report/stock/store/")
                        else TITLES.get(typ, typ)
                    ),
                    "count": count,
                    "archived": int(archived or 0),
                }
            )
    last = (
        db.query(ImportRun)
        .filter(ImportRun.summary["source"].as_string() == "moysklad")
        .order_by(ImportRun.id.desc())
        .first()
    )
    return {
        "categories": sorted(result, key=lambda r: r["title"]),
        "imported_at": last.imported_at if last else None,
        "source_completed_at": last.summary.get("source_completed_at") if last else None,
        "warnings": last.summary.get("warnings", []) if last and user.role == UserRole.ADMIN else [],
    }


def search(db, user, query="", entity_type=None, archive="active", limit=50, offset=0):
    types = [r[0] for r in db.query(SourceRecord.entity_type).distinct() if allowed(user, r[0])]
    if entity_type and entity_type not in types:
        raise HTTPException(403, "Нет доступа к этим данным")
    stmt = db.query(SourceRecord).filter(
        SourceRecord.entity_type.in_([entity_type] if entity_type else types)
    )
    if archive != "all":
        stmt = stmt.filter(SourceRecord.archived.is_(archive == "archived"))
    for word in query.casefold().split()[:8]:
        stmt = stmt.filter(SourceRecord.search_text.contains(word, autoescape=True))
    count = stmt.count()
    items = stmt.order_by(SourceRecord.title, SourceRecord.id).offset(offset).limit(limit).all()
    return {"total": count, "items": [brief(r) for r in items]}


def brief(r):
    p = r.payload
    return {
        "id": r.id,
        "type": r.entity_type,
        "source_id": r.source_id,
        "title": r.title,
        "archived": r.archived,
        "state": r.state,
        "code": p.get("code"),
        "moment": p.get("moment"),
        "sum_rub": float(p["sum"]) / 100 if "sum" in p else None,
        "applicable": p.get("applicable"),
        "agent": p.get("agent", {}).get("name"),
        "imported_at": r.imported_at,
    }


def get_record(db, user, record_id):
    record = db.get(SourceRecord, record_id)
    if record is None:
        raise HTTPException(404, "Запись не найдена")
    if not allowed(user, record.entity_type):
        raise HTTPException(403, "Нет доступа к этим данным")
    return record


def attachment_path(record, index):
    files = record.payload.get("_downloaded_files", [])
    if index < 0 or index >= len(files):
        raise HTTPException(404, "Файл не найден")
    item = files[index]
    root = (Path(settings.storage_dir) / "moysklad" / "files").resolve()
    target = (root / item["local_name"]).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "Файл не найден")
    return target, item
