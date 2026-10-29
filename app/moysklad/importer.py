"""Import a GET-only MoySklad export in one transaction; --dry-run rolls back.

Raw money stays in kopecks in the replica. Native accounting uses rubles.
Source links make retries idempotent. Operational workflows are never replayed.
"""

from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session
from app.moysklad.models import SourceRecord, SourceLink, ImportRun
from app.accounting.models import (
    Organization,
    BankAccount,
    Counterparty,
    CounterpartyKind,
    MoneyMovement,
    MoneyDirection,
    MoneySubkind,
    MoneyAssessment,
    MoneyMovementStatus,
    MoneySourceKind,
)
from app.warehouse.models import (
    WarehouseMaterial,
    Warehouse,
    MaterialCategory,
    StockMovement,
    StockMovementReason,
    Supplier,
    SupplierStatus,
)
from app.clients.models import Client
from app.users.models import User, UserRole


def ref(value):
    if not isinstance(value, dict):
        return ""
    return value.get("id") or value.get("meta", {}).get("href", "").split("?")[0].rstrip("/").split("/")[-1]


def norm(value):
    return re.sub(r"[^а-яa-z0-9]", "", str(value or "").casefold().replace("ё", "е"))


def rub(value):
    return Decimal(str(value or 0)) / 100


def moment(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Europe/Moscow")) if value else None


def rows(value):
    return value.get("rows", []) if isinstance(value, dict) else value if isinstance(value, list) else []


def import_export(db: Session, data: dict, metadata: dict, reports: dict, manifest: dict, owner_id: int):
    owner = db.get(User, owner_id)
    if not owner or not owner.is_active or owner.role != UserRole.ADMIN:
        raise ValueError("An active owner administrator is required")
    if any(data.get(t) for t in ("product", "variant", "bundle")) and "report/stock/bystore" not in reports:
        raise ValueError("Full stock report is required before changing balances")
    previous = (
        db.query(ImportRun)
        .filter(ImportRun.summary["source"].as_string() == "moysklad")
        .order_by(ImportRun.id.desc())
        .first()
    )
    same_snapshot = previous and previous.summary.get("source_completed_at") == manifest.get("completed_at")
    from app.warehouse.reservation_models import StockReservation

    if not same_snapshot and db.query(StockReservation.id).first():
        raise ValueError(
            "Local reservations require delta reconciliation; a stock snapshot cannot replace their balances"
        )
    for typ, count in manifest.get("counts", {}).items():
        if len(data.get(typ, [])) != count:
            raise ValueError("Incomplete export: " + typ)
    stats = Counter()
    warnings = []
    index = {}

    def index_rows(obj):
        if isinstance(obj, dict):
            href = obj.get("meta", {}).get("href", "").split("?")[0]
            if href and obj.get("name"):
                index[href] = obj["name"]
            for k, v in obj.items():
                if k != "meta":
                    index_rows(v)
        elif isinstance(obj, list):
            for v in obj:
                index_rows(v)

    index_rows(data)
    index_rows(metadata)

    def decorate(obj):
        if isinstance(obj, dict):
            result = {
                k: decorate(v)
                for k, v in obj.items()
                if k.casefold()
                not in {"password", "accesstoken", "refreshtoken", "authtoken", "clientsecret"}
            }
            name = index.get(obj.get("meta", {}).get("href", "").split("?")[0])
            if name and not result.get("name"):
                result["name"] = name
            return result
        if isinstance(obj, list):
            return [decorate(v) for v in obj]
        return obj

    existing = {(r.entity_type, r.source_id): r for r in db.query(SourceRecord)}
    source_data = {
        **data,
        **{path: rows_ if isinstance(rows_, list) else [rows_] for path, rows_ in reports.items()},
        "metadata": [{"id": "metadata", "name": "Справочники и статусы МойСклада", "data": metadata}],
    }
    for typ, items in source_data.items():
        for raw in items:
            payload = decorate(raw)
            sid = ref(raw) or hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
            key = (typ, sid)
            record = existing.get(key)
            if record is None:
                record = SourceRecord(entity_type=typ, source_id=sid)
                db.add(record)
                existing[key] = record
                stats["records_created"] += 1
            elif record.payload == payload:
                stats["records_unchanged"] += 1
            else:
                stats["records_updated"] += 1
            record.title = str(
                raw.get("name")
                or index.get(raw.get("meta", {}).get("href", "").split("?")[0])
                or typ + " " + sid
            )[:1000]
            record.archived = bool(raw.get("archived", False))
            record.state = payload.get("state", {}).get("name")
            record.payload = payload
            record.search_text = json.dumps(payload, ensure_ascii=False).casefold()
            record.imported_at = datetime.now(timezone.utc)
    # Working copies are editable locally and never overwritten on retries.
    from app.moysklad.recipes import Recipe

    catalogue = {r["id"]: r for typ in ("product", "variant", "bundle", "service") for r in data.get(typ, [])}
    recipes = {r.source_id: r for r in db.query(Recipe) if r.source_id}
    for raw in data.get("processingplan", []):
        if raw["id"] in recipes:
            continue
        content = {"inputs": [], "outputs": [], "stages": decorate(rows(raw.get("stages"))), "warnings": []}
        for source, target in [("materials", "inputs"), ("products", "outputs")]:
            for line in rows(raw.get(source)):
                product = line.get("assortment") or line.get("product") or {}
                href = product.get("meta", {}).get("href", "").split("?")[0]
                quantity = Decimal(str(line.get("quantity", 0)))
                if quantity <= 0:
                    content["warnings"].append(
                        "Проверьте количество у позиции " + index.get(href, ref(product))
                    )
                    continue
                card = catalogue.get(ref(product), {})
                parent = catalogue.get(ref(card.get("product")), {})
                unit = card.get("uom") or parent.get("uom") or {}
                unit_name = unit.get("name") or index.get(
                    unit.get("meta", {}).get("href", "").split("?")[0], ""
                )
                content[target].append(
                    {
                        "source_id": ref(product),
                        "material_id": None,
                        "title": index.get(href, ref(product))[:255],
                        "unit": unit_name[:32],
                        "quantity": str(quantity),
                    }
                )
        recipe = Recipe(
            source_id=raw["id"], title=raw["name"][:255], archived=bool(raw.get("archived")), content=content
        )
        db.add(recipe)
        recipes[raw["id"]] = recipe
        stats["recipes_created"] += 1
    if same_snapshot:
        # Refresh downloaded files/labels, but never reset stock or edited
        # accounting entries when retrying the very same migration snapshot.
        db.flush()
        return {**previous.summary, "changes": dict(stats), "native_replay_skipped": True}
    links = {(x.source_key, x.target_type): x for x in db.query(SourceLink)}

    def lookup(key, cls):
        link = links.get((key, cls.__tablename__))
        return db.get(cls, link.target_id) if link else None

    def remember(key, obj):
        db.flush()
        pair = (key, obj.__tablename__)
        if pair not in links:
            link = SourceLink(source_key=key, target_type=obj.__tablename__, target_id=obj.id)
            db.add(link)
            links[pair] = link

    def unique(items):
        return items[0] if len(items) == 1 else None

    def existing_by_name(cls, value):
        return unique(
            [x for x in db.query(cls) if norm(getattr(x, "name", getattr(x, "full_name", ""))) == norm(value)]
        )

    orgs = {}
    accounts = {}
    cpmap = {}
    for raw in data.get("organization", []):
        key = "organization/" + raw["id"]
        obj = lookup(key, Organization)
        if obj is None:
            matches = [
                o
                for o in db.query(Organization)
                if (raw.get("inn") and o.inn == raw["inn"])
                or norm(o.name.replace("ООО", "")) == norm(raw["name"].replace("ООО", ""))
            ]
            obj = unique(matches)
        if obj is None:
            obj = Organization(name=raw["name"], short_name=raw["name"][:64])
            db.add(obj)
            stats["organizations_created"] += 1
        obj.inn = raw.get("inn") or obj.inn
        obj.is_active = not raw.get("archived", False)
        remember(key, obj)
        orgs[raw["id"]] = obj
        for a in rows(raw.get("accounts")):
            key = "account/" + a["id"]
            account = lookup(key, BankAccount)
            if account is None:
                account = unique(
                    [
                        x
                        for x in db.query(BankAccount).filter_by(organization_id=obj.id)
                        if a.get("accountNumber") and x.account_number == a["accountNumber"]
                    ]
                )
            if account is None:
                account = BankAccount(
                    organization_id=obj.id,
                    name=a.get("bankName") or a.get("accountNumber") or "Счёт МойСклад",
                    is_default=False,
                )
                db.add(account)
                stats["accounts_created"] += 1
            account.account_number = a.get("accountNumber")
            account.bank_name = a.get("bankName")
            account.currency = "RUB"
            remember(key, account)
            accounts[a["id"]] = account
    supplier_ids = {ref(row.get("agent")) for typ in ("purchaseorder", "supply") for row in data.get(typ, [])}
    for raw in data.get("counterparty", []):
        key = "counterparty/" + raw["id"]
        obj = lookup(key, Counterparty)
        if obj is None:
            obj = unique(
                [x for x in db.query(Counterparty) if (raw.get("inn") and x.inn == raw["inn"])]
            ) or existing_by_name(Counterparty, raw["name"])
        if obj is None:
            obj = Counterparty(name=raw["name"])
            db.add(obj)
            stats["counterparties_created"] += 1
        obj.inn = raw.get("inn") or obj.inn
        obj.is_active = not raw.get("archived", False)
        if not obj.client_id:
            client = existing_by_name(Client, raw["name"])
            if client:
                obj.client_id = client.id
                obj.kind = CounterpartyKind.CLIENT
        if raw["id"] in supplier_ids and not obj.client_id:
            supplier = lookup(key, Supplier) or existing_by_name(Supplier, raw["name"])
            if supplier is None:
                supplier = Supplier(name=raw["name"], contacts=[], categories=[])
                db.add(supplier)
                stats["suppliers_created"] += 1
            supplier.status = SupplierStatus.ARCHIVED if raw.get("archived") else SupplierStatus.ACTIVE
            # Imported contacts fill an empty card; employee edits remain intact.
            if not supplier.contacts:
                supplier.contacts = [{"kind": k, "value": raw[k]} for k in ("phone", "email") if raw.get(k)]
            remember(key, supplier)
            obj.supplier_id = supplier.id
            obj.kind = CounterpartyKind.SUPPLIER
        if not obj.comment:
            obj.comment = raw.get("description")
        remember(key, obj)
        cpmap[raw["id"]] = obj
    # Snapshot balances are applied once, never replaying historical deliveries.
    stocks = {}
    for raw in reports.get("report/stock/bystore", []):
        for balance in raw.get("stockByStore", []):
            stocks[(ref(raw), ref(balance))] = balance.get("stock", 0)
    stores = {r["id"]: r for r in data.get("store", [])}
    valuations = {
        (ref(row), sid): row["price"]
        for sid in stores
        for row in reports.get("report/stock/store/" + sid, [])
        if "price" in row
    }
    products = {r["id"]: r for typ in ("product", "variant", "bundle") for r in data.get(typ, [])}
    fractional_ids = {
        ref(line.get("assortment") or line.get("product"))
        for plan in data.get("processingplan", [])
        for field in ("materials", "products")
        for line in rows(plan.get(field))
        if Decimal(str(line.get("quantity", 0))) % 1
    }
    code_counts = Counter(str(r.get("code")) for r in products.values() if r.get("code"))
    title_counts = Counter(norm(r.get("name")) for r in products.values())
    original_materials = db.query(WarehouseMaterial).all()
    title_candidates = {}
    for m in original_materials:
        title_candidates.setdefault((m.warehouse, norm(m.title)), []).append(m)
    for raw in products.values():
        parent = products.get(ref(raw.get("product")), {})
        for store_id, store in stores.items():
            try:
                warehouse = Warehouse(store["name"])
            except ValueError:
                warnings.append("Unmapped store " + store["name"])
                continue
            key = "stock/" + raw["id"] + "/" + store_id
            obj = lookup(key, WarehouseMaterial)
            code = str(raw.get("code") or "MS-" + raw["id"])[:64]
            if obj is None:
                candidate = db.query(WarehouseMaterial).filter_by(warehouse=warehouse, code=code).first()
                claimed = candidate and any(
                    l.target_type == "warehouse_materials"
                    and l.target_id == candidate.id
                    and l.source_key != key
                    for l in links.values()
                )
                if (
                    candidate
                    and not claimed
                    and (code_counts.get(code) == 1 or norm(candidate.title) == norm(raw["name"]))
                ):
                    obj = candidate
                elif candidate:
                    code = "MS-" + raw["id"]
            if obj is None and title_counts[norm(raw["name"])] == 1:
                candidates = [
                    m
                    for m in title_candidates.get((warehouse, norm(raw["name"])), [])
                    if not any(
                        l.target_type == "warehouse_materials" and l.target_id == m.id for l in links.values()
                    )
                ]
                if len(candidates) == 1:
                    obj = candidates[0]
            if obj is None:
                obj = WarehouseMaterial(
                    warehouse=warehouse,
                    code=code,
                    title=raw["name"][:255],
                    unit="шт",
                    category=MaterialCategory.NONE,
                    quantity_in_stock=0,
                )
                db.add(obj)
                stats["materials_created"] += 1
            prior = Decimal(str(obj.quantity_in_stock or 0))
            new = Decimal(str(stocks.get((raw["id"], store_id), 0)))
            obj.title = raw["name"][:255]
            obj.is_archived = bool(raw.get("archived", False) or parent.get("archived", False))
            obj.catalog_path = (raw.get("pathName") or parent.get("pathName") or "")[:1000] or None
            obj.unit = (raw.get("uom", {}).get("name") or parent.get("uom", {}).get("name") or obj.unit)[:32]
            obj.purchase_price = rub(
                valuations.get(
                    (raw["id"], store_id), raw.get("buyPrice", parent.get("buyPrice", {})).get("value", 0)
                )
            )
            obj.is_fractional = (
                bool(new % 1)
                or raw["id"] in fractional_ids
                or obj.is_fractional
                or norm(obj.unit) in {"м", "м2", "м3", "кг", "г", "л", "мл", "погм", "мп"}
            )
            path = norm(raw.get("pathName") or parent.get("pathName"))
            for cat in MaterialCategory:
                if cat != MaterialCategory.NONE and norm(cat.value) == path:
                    obj.category = cat
            obj.quantity_in_stock = new
            remember(key, obj)
            if prior != new:
                db.add(
                    StockMovement(
                        warehouse_material_id=obj.id,
                        delta=new - prior,
                        reason=StockMovementReason.MANUAL_ADJUST,
                        created_by_id=owner_id,
                        balance_after=new,
                        note="Сверка остатков с МойСкладом; " + manifest.get("completed_at", "")[:40],
                    )
                )
                stats["balances_changed"] += 1
    linked_materials = {l.target_id for l in links.values() if l.target_type == "warehouse_materials"}
    unmatched = [m.id for m in original_materials if m.id not in linked_materials]
    if unmatched:
        warnings.append(
            str(len(unmatched))
            + " прежних складских карточек не сопоставлены с источником и сохранены для сверки"
        )
    # Native financial ledger reflects source status; no second posting side effects.
    for typ in ("paymentin", "paymentout", "cashin", "cashout"):
        for raw in data.get(typ, []):
            key = typ + "/" + raw["id"]
            obj = lookup(key, MoneyMovement)
            organization = orgs.get(ref(raw.get("organization")))
            account = accounts.get(ref(raw.get("organizationAccount")))
            if account is None and organization:
                account_key = (
                    "cash/" + str(organization.id)
                    if typ.startswith("cash")
                    else "unassigned-account/" + str(organization.id)
                )
                account = lookup(account_key, BankAccount)
                if account is None:
                    account = BankAccount(
                        organization_id=organization.id,
                        name="Касса МойСклад" if typ.startswith("cash") else "Счёт не указан в МойСкладе",
                        is_default=False,
                    )
                    db.add(account)
                    remember(account_key, account)
            if account is None:
                warnings.append("Payment without mapped organization: " + key)
                continue
            incoming = typ in ("paymentin", "cashin")
            cp = cpmap.get(ref(raw.get("agent")))
            if obj is None:
                obj = MoneyMovement(initiator_id=owner_id, source_kind=MoneySourceKind.NONE)
                db.add(obj)
                stats["payments_created"] += 1
            obj.direction = MoneyDirection.INCOME if incoming else MoneyDirection.EXPENSE
            obj.subkind = MoneySubkind.OTHER_INCOME if incoming else MoneySubkind.OTHER_EXPENSE
            obj.amount = rub(raw.get("sum"))
            obj.tax = rub(raw.get("vatSum"))
            obj.currency = "RUB"
            obj.assessment = MoneyAssessment.ACTUAL
            obj.status = MoneyMovementStatus.POSTED if raw.get("applicable") else MoneyMovementStatus.DRAFT
            obj.doc_date = moment(raw.get("moment"))
            obj.posted_at = obj.doc_date if raw.get("applicable") else None
            obj.account_id = account.id
            obj.counterparty_id = cp.id if cp else None
            obj.payment_purpose = (raw.get("paymentPurpose") or raw.get("description") or "")[:500] or None
            obj.comment = (
                "МойСклад · "
                + typ
                + " № "
                + str(raw.get("name", ""))
                + "\n"
                + str(raw.get("description", ""))
            )
            obj.external_number = str(raw.get("incomingNumber") or raw.get("name") or "")[:255]
            obj.link = raw.get("meta", {}).get("uuidHref")
            obj.affects_profit = True
            remember(key, obj)
    db.flush()
    summary = {
        "source": "moysklad",
        "source_completed_at": manifest.get("completed_at"),
        "counts": {k: len(v) for k, v in data.items()},
        "archived": {k: sum(bool(r.get("archived")) for r in v) for k, v in data.items()},
        "changes": dict(stats),
        "warnings": sorted(set(warnings)),
        "export_errors": manifest.get("errors", []),
        "attachments": manifest.get("attachments", {}),
    }
    db.add(ImportRun(summary=summary))
    db.flush()
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from app.db import import_all_models  # noqa
    from app.db.session import SessionLocal

    root = Path(args.directory)
    load = lambda name: json.loads((root / (name + ".json")).read_text(encoding="utf-8"))
    with SessionLocal() as db:
        result = import_export(
            db, load("entities"), load("metadata"), load("reports"), load("manifest"), args.owner_id
        )
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
