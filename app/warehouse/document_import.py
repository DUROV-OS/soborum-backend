"""Expose historical stock documents without replaying any source movements."""

from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.moysklad.models import SourceRecord, SourceLink
from app.moysklad.importer import ref, moment, rows
from app.warehouse.models import StockDocument, WarehouseMaterial


def import_history(db: Session, actor_id: int):
    stores = {
        r.source_id: r.title
        for r in db.query(SourceRecord).filter_by(entity_type="store")
    }
    material_links = {
        l.source_key: l.target_id
        for l in db.query(SourceLink).filter_by(target_type="warehouse_materials")
    }
    materials = {m.id: m for m in db.query(WarehouseMaterial)}
    known = {(r.kind, r.source_id) for r in db.query(StockDocument) if r.source_id}
    counts = {"transfer": 0, "inventory": 0}
    for raw_record in db.query(SourceRecord).filter(
        SourceRecord.entity_type.in_(["move", "inventory"])
    ):
        raw = raw_record.payload
        kind = "transfer" if raw_record.entity_type == "move" else "inventory"
        if (kind, raw_record.source_id) in known:
            continue
        sid = ref(raw.get("sourceStore") if kind == "transfer" else raw.get("store"))
        target_sid = ref(raw.get("targetStore"))
        lines = []
        for position in rows(raw.get("positions")):
            assortment = position.get("assortment", {})
            aid = ref(assortment)
            mid = material_links.get("stock/" + aid + "/" + sid)
            material = materials.get(mid)
            lines.append(
                {
                    "material_id": mid,
                    "target_material_id": (
                        material_links.get("stock/" + aid + "/" + target_sid)
                        if target_sid
                        else None
                    ),
                    "title": (
                        material.title if material else assortment.get("name") or aid
                    ),
                    "code": material.code if material else assortment.get("code", ""),
                    "unit": material.unit if material else "",
                    "is_fractional": material.is_fractional if material else True,
                    "expected_quantity": (
                        str(position.get("calculatedQuantity"))
                        if "calculatedQuantity" in position
                        else None
                    ),
                    "quantity": str(position.get("quantity", 0)),
                    "delta": (
                        str(position.get("correctionAmount"))
                        if "correctionAmount" in position
                        else None
                    ),
                    "unit_price": str((position.get("price") or 0) / 100),
                    "source_assortment_id": aid,
                }
            )
        db.add(
            StockDocument(
                kind=kind,
                status="historical",
                number=raw_record.title,
                warehouse=stores.get(sid, sid or "Склад не указан"),
                target_warehouse=stores.get(target_sid, target_sid) or None,
                occurred_at=moment(raw.get("moment") or raw.get("created"))
                or datetime.now(timezone.utc),
                note=raw.get("description") or "",
                lines=lines,
                operation_ids=[],
                source_id=raw_record.source_id,
                source_state=raw_record.state
                or ("Проведён" if raw.get("applicable") else None),
                archived=raw_record.archived,
                actor_id=actor_id,
                parameters={"source_applied": raw.get("applicable")},
                version=1,
            )
        )
        counts[kind] += 1
    db.flush()
    return counts
