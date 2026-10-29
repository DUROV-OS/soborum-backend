"""Build editable purchasing documents from the existing private source replica.

Historical receipts are document history: current stock was already reconciled
by the main migration, so importing them must never post stock a second time.
"""

from decimal import Decimal
from app.accounting.models import SupplierOrder, SupplierOrderStatus
from app.moysklad.models import SourceRecord, SourceLink
from app.moysklad.importer import ref, rows, rub, moment
from app.purchasing.models import PurchaseReceipt
from app.warehouse.models import Supplier, SupplierStatus, WarehouseMaterial
from app.users.models import User, UserRole


def import_native(db, owner_id):
    owner = db.get(User, owner_id)
    if not owner or not owner.is_active or owner.role != UserRole.ADMIN:
        raise ValueError("Active owner required")
    source = {
        typ: db.query(SourceRecord).filter_by(entity_type=typ).all()
        for typ in ["purchaseorder", "supply", "counterparty"]
    }
    counterparties = {r.source_id: r for r in source["counterparty"]}
    links = {(r.source_key, r.target_type): r.target_id for r in db.query(SourceLink)}
    material_units = {m.id: m.unit for m in db.query(WarehouseMaterial)}
    suppliers = {}
    created = {"orders": 0, "receipts": 0, "suppliers": 0}

    def supplier(raw):
        sid = ref(raw.get("agent"))
        if sid in suppliers:
            return suppliers[sid]
        target = links.get(("counterparty/" + sid, "suppliers"))
        if target is None:
            agent = counterparties.get(sid)
            if agent is None:
                raise ValueError("Unmapped source supplier: " + sid)
            row = Supplier(
                name=agent.title[:255],
                contacts=[],
                categories=[],
                status=SupplierStatus.ARCHIVED if agent.archived else SupplierStatus.ACTIVE,
            )
            db.add(row)
            db.flush()
            target = row.id
            db.add(SourceLink(source_key="counterparty/" + sid, target_type="suppliers", target_id=target))
            created["suppliers"] += 1
        suppliers[sid] = target
        return target

    def line(raw, store, document):
        a = raw.get("assortment") or {}
        mid = links.get(("stock/" + ref(a) + "/" + store, "warehouse_materials"))
        price = rub(raw.get("price")) * (1 - Decimal(str(raw.get("discount", 0))) / 100)
        if document.get("vatEnabled") and not document.get("vatIncluded"):
            price *= 1 + Decimal(str(raw.get("vat") or 0)) / 100
        return {
            "material": str(a.get("name") or ref(a)),
            "category": None,
            "warehouse_material_id": mid,
            "quantity": float(raw.get("quantity", 0)),
            "unit_price": float(price),
            "received_quantity": float(raw.get("shipped") or 0),
            "unit": (a.get("uom") or {}).get("name") or material_units.get(mid, ""),
            "stock_tracked": (a.get("meta") or {}).get("type") != "service",
        }

    orders = {r.source_id: r for r in db.query(SupplierOrder).filter(SupplierOrder.source_id.is_not(None))}
    for source_row in source["purchaseorder"]:
        if source_row.source_id in orders:
            continue
        raw = source_row.payload
        items = [line(p, ref(raw.get("store")), raw) for p in rows(raw.get("positions"))]
        complete = bool(items) and all(p["received_quantity"] >= p["quantity"] for p in items)
        partial = any(p["received_quantity"] for p in items)
        row = SupplierOrder(
            source_id=source_row.source_id,
            number=source_row.title[:255],
            source_state=source_row.state,
            archived=source_row.archived,
            supplier_id=supplier(raw),
            items=items,
            total_cost=rub(raw.get("sum")),
            opening_paid=rub(raw.get("payedSum")),
            currency="RUB",
            expected_at=(
                moment(raw.get("deliveryPlannedMoment")).date() if raw.get("deliveryPlannedMoment") else None
            ),
            status=(
                SupplierOrderStatus.RECEIVED
                if complete
                else SupplierOrderStatus.IN_TRANSIT if partial else SupplierOrderStatus.ORDERED
            ),
            comment=raw.get("description") or None,
            created_at=moment(raw.get("moment")),
        )
        db.add(row)
        db.flush()
        orders[source_row.source_id] = row
        created["orders"] += 1
    receipts = {r.source_id for r in db.query(PurchaseReceipt).filter(PurchaseReceipt.source_id.is_not(None))}
    for source_row in source["supply"]:
        if source_row.source_id in receipts:
            continue
        raw = source_row.payload
        order = orders.get(ref(raw.get("purchaseOrder")))
        entries = [line(p, ref(raw.get("store")), raw) for p in rows(raw.get("positions"))]
        for p in entries:
            p["title"] = p.pop("material")
            p["material_id"] = p.pop("warehouse_material_id")
        db.add(
            PurchaseReceipt(
                source_id=source_row.source_id,
                order_id=order.id if order else None,
                supplier_id=supplier(raw),
                actor_id=owner_id,
                number=source_row.title[:255],
                status="posted" if raw.get("applicable") else "draft",
                occurred_at=moment(raw.get("moment")),
                lines=entries,
                note=raw.get("description") or "",
                parameters={"imported_history": True},
            )
        )
        created["receipts"] += 1
    db.flush()
    from app.warehouse.service import recalculate_supplier_totals

    for supplier_id in set(suppliers.values()):
        recalculate_supplier_totals(db, supplier_id)
    return created


if __name__ == "__main__":
    import argparse
    from app.db import import_all_models
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-id", type=int, required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        print(import_native(db, args.owner_id))
        db.commit()
