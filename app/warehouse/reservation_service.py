"""All stock writers lock materials before reading or changing their holds."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException
from sqlalchemy import func
from app.warehouse.reservation_models import StockReservation, ReservationEvent

REMAINING = (
    StockReservation.quantity
    - StockReservation.released_quantity
    - StockReservation.consumed_quantity
)


def dec(value):
    return Decimal(str(value or 0))


def remaining(row):
    return dec(row.quantity) - dec(row.released_quantity) - dec(row.consumed_quantity)


def invalidate_cache(db, production_id=None):
    from app.ai.models import AiCacheEntry

    db.query(AiCacheEntry).filter(
        AiCacheEntry.key.in_(
            ["section_analytics:warehouse", "dashboard_section_signal:warehouse"]
        )
    ).delete(synchronize_session=False)
    if production_id:
        from app.production.readiness import invalidate_production_caches

        invalidate_production_caches(db, production_id)


def allocations(db, ids):
    result = {}
    for mid, pid, quantity in (
        db.query(
            StockReservation.material_id,
            StockReservation.production_id,
            func.sum(REMAINING),
        )
        .filter(StockReservation.material_id.in_(ids), REMAINING > 0)
        .group_by(StockReservation.material_id, StockReservation.production_id)
    ):
        result.setdefault(mid, {})[pid] = dec(quantity)
    return result


def available(db, material, production_id=None):
    groups = allocations(db, [material.id]).get(material.id, {})
    held = sum(groups.values(), Decimal(0))
    own = (
        groups.get(production_id, Decimal(0))
        if production_id is not None
        else Decimal(0)
    )
    return dec(material.quantity_in_stock) - held + own


def assert_available(db, lines, production_id=None, code=409):
    required = defaultdict(Decimal)
    materials = {}
    for m, q in lines:
        required[m.id] += dec(q)
        materials[m.id] = m
    holds = allocations(db, list(materials))
    missing = []
    for mid, q in required.items():
        q = q.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        groups = holds.get(mid, {})
        own = (
            groups.get(production_id, Decimal(0))
            if production_id is not None
            else Decimal(0)
        )
        free = (
            dec(materials[mid].quantity_in_stock)
            - sum(groups.values(), Decimal(0))
            + own
        )
        if q > free:
            missing.append(
                materials[mid].title
                + f" (доступно {max(Decimal(0),free):f} {materials[mid].unit})"
            )
    if missing:
        raise HTTPException(
            code,
            "Недостаточно свободного остатка с учётом резервов: " + "; ".join(missing),
        )


def consume_for_production(
    db, material_id, production_id, quantity, actor_id, movement_id
):
    if production_id is None:
        return
    pending = dec(quantity).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    rows = (
        db.query(StockReservation)
        .filter(
            StockReservation.material_id == material_id,
            StockReservation.production_id == production_id,
            REMAINING > 0,
        )
        .order_by(StockReservation.id)
        .populate_existing()
        .with_for_update(of=StockReservation)
        .all()
    )
    for row in rows:
        if pending <= 0:
            break
        take = min(pending, remaining(row))
        row.consumed_quantity = dec(row.consumed_quantity) + take
        row.version += 1
        pending -= take
        db.add(
            ReservationEvent(
                reservation_id=row.id,
                kind="issued",
                quantity=take,
                actor_id=actor_id,
                movement_id=movement_id,
                note="Выдача дому по техкарте или заявке",
                parameters={},
            )
        )
    db.flush()

    if rows:
        invalidate_cache(db, production_id)


def demand_with_holds(breakdown, groups):
    requested = defaultdict(Decimal)
    for item in breakdown:
        requested[item.production_id] += dec(item.quantity_requested)
    held = sum(groups.values(), Decimal(0))
    overlap = sum(
        (min(q, groups.get(pid, Decimal(0))) for pid, q in requested.items()),
        Decimal(0),
    )
    return sum(requested.values(), Decimal(0)) + held - overlap
