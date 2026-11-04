"""Create local sales history from the private replica, without replaying stock/money."""

from collections import defaultdict
from decimal import Decimal
from app.moysklad.models import SourceRecord, SourceLink
from app.moysklad.importer import ref, rows, rub, moment
from app.sales.models import SalesOrder, SalesShipment
from app.warehouse.reservation_models import StockReservation
from app.warehouse.reservation_service import dec
from app.warehouse.models import WarehouseMaterial
from app.users.models import User, UserRole


def import_native(db, owner_id):
    user = db.get(User, owner_id)
    if not user or not user.is_active or user.role != UserRole.ADMIN:
        raise ValueError("Active owner required")
    links = {(l.source_key, l.target_type): l.target_id for l in db.query(SourceLink)}
    sources = {
        t: db.query(SourceRecord).filter_by(entity_type=t).all()
        for t in (
            "customerorder",
            "invoiceout",
            "demand",
            "product",
            "variant",
            "service",
            "bundle",
        )
    }
    assortment = {
        s.source_id: s
        for t in ("product", "variant", "service", "bundle")
        for s in sources[t]
    }
    materials = {m.id: m for m in db.query(WarehouseMaterial)}
    invoices = {s.source_id: s for s in sources["invoiceout"]}
    orders = {
        r.source_id: r
        for r in db.query(SalesOrder).filter(SalesOrder.source_id.is_not(None))
    }
    result = {
        "orders": 0,
        "shipments": 0,
        "converted_reservations": 0,
        "unlinked_shipments": 0,
    }

    def target(raw, key, table):
        id = links.get(
            (key + "/" + ref(raw.get(key if key != "counterparty" else "agent")), table)
        )
        if not id:
            raise ValueError(
                "Unmapped sales " + key + " in document " + str(raw.get("id"))
            )
        return id

    def line(raw, doc):
        a = raw.get("assortment") or {}
        sid = ref(a)
        src = assortment.get(sid)
        mid = links.get(
            ("stock/" + sid + "/" + ref(doc.get("store")), "warehouse_materials")
        )
        material = materials.get(mid)
        tracked = (a.get("meta") or {}).get("type") != "service"
        price = rub(raw.get("price")) * (1 - dec(raw.get("discount")) / 100)
        if doc.get("vatEnabled") and not doc.get("vatIncluded"):
            price *= 1 + dec(raw.get("vat")) / 100
        return {
            "material_id": mid if tracked else None,
            "title": (
                src.title
                if src
                else a.get("name") or (material.title if material else sid)
            )[:255],
            "unit": (
                material.unit if material else (a.get("uom") or {}).get("name") or ""
            ),
            "stock_tracked": tracked,
            "quantity": str(dec(raw.get("quantity"))),
            "unit_price": str(price),
            "line_total": str(
                (dec(raw.get("quantity")) * price).quantize(Decimal("0.01"))
            ),
            "opening_shipped": str(dec(raw.get("shipped"))),
            "source_line_id": raw.get("id"),
        }

    for src in sources["customerorder"]:
        if src.source_id in orders:
            continue
        raw = src.payload
        entries = [line(p, raw) for p in rows(raw.get("positions"))]
        holds = db.query(StockReservation).filter_by(source_order_id=src.id).all()
        # Old manual issues have no SalesShipment. Preserve their fulfilment
        # as an opening quantity rather than issuing the same goods again.
        for h in holds:
            entries[h.source_line]["opening_shipped"] = str(
                dec(entries[h.source_line]["opening_shipped"])
                + dec(h.consumed_quantity)
            )
        if any(dec(e["opening_shipped"]) > dec(e["quantity"]) for e in entries):
            raise ValueError("Source/order fulfilment requires reconciliation")
        row = SalesOrder(
            source_id=src.source_id,
            source_record_id=src.id,
            number=src.title[:255],
            counterparty_id=target(raw, "counterparty", "counterparties"),
            organization_id=target(raw, "organization", "organizations"),
            status="confirmed" if raw.get("applicable") else "draft",
            source_state=src.state,
            archived=src.archived,
            currency="RUB",
            total=rub(raw.get("sum")),
            opening_paid=rub(raw.get("payedSum")),
            lines=entries,
            expected_at=(
                moment(raw["deliveryPlannedMoment"]).date()
                if raw.get("deliveryPlannedMoment")
                else None
            ),
            address=raw.get("shipmentAddress") or "",
            note=raw.get("description") or "",
            actor_id=owner_id,
            occurred_at=moment(raw.get("moment")),
            parameters={"imported_history": True},
        )
        db.add(row)
        db.flush()
        for h in holds:
            h.sales_order_id = row.id
            h.sales_line = h.source_line
            h.source_order_id = None
            h.source_line = None
            result["converted_reservations"] += 1
        orders[src.source_id] = row
        result["orders"] += 1

    done = {
        r.source_id
        for r in db.query(SalesShipment).filter(SalesShipment.source_id.is_not(None))
    }
    # The export's demands refer to invoices, which in turn refer to orders.
    # Only an explicit, unique source link establishes this relation.
    by_demand = defaultdict(set)
    for inv in sources["invoiceout"]:
        oid = ref(inv.payload.get("customerOrder"))
        for d in rows(inv.payload.get("demands")):
            if oid:
                by_demand[ref(d)].add(oid)
    for src in sources["demand"]:
        if src.source_id in done:
            continue
        raw = src.payload
        candidates = set(by_demand[src.source_id])
        direct = ref(raw.get("customerOrder"))
        if direct:
            candidates.add(direct)
        for link in rows(raw.get("invoicesOut")):
            inv = invoices.get(ref(link))
            if inv:
                oid = ref(inv.payload.get("customerOrder"))
                if oid:
                    candidates.add(oid)
        if len(candidates) > 1:
            raise ValueError("Ambiguous source shipment/order links")
        order = orders.get(next(iter(candidates), ""))
        if not order:
            result["unlinked_shipments"] += 1
        db.add(
            SalesShipment(
                source_id=src.source_id,
                order_id=order.id if order else None,
                counterparty_id=target(raw, "counterparty", "counterparties"),
                organization_id=target(raw, "organization", "organizations"),
                number=src.title[:255],
                status="posted" if raw.get("applicable") else "draft",
                historical=True,
                archived=src.archived,
                currency="RUB",
                total=rub(raw.get("sum")),
                lines=[line(p, raw) for p in rows(raw.get("positions"))],
                received_by="",
                address=raw.get("shipmentAddress") or "",
                note=raw.get("description") or "",
                actor_id=owner_id,
                occurred_at=moment(raw.get("moment")),
                parameters={"imported_history": True},
            )
        )
        result["shipments"] += 1
    db.flush()
    return result


if __name__ == "__main__":
    import argparse
    from app.db import import_all_models
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        result = import_native(db, args.owner_id)
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
        print(result)
