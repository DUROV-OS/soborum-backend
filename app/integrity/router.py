import json

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.files import FileAsset, file_integrity
from app.core.deps import require_admin
from app.db.session import get_db
from app.integrity.models import IntegrityEvent
from app.integrity.service import verify

app = FastAPI(title="История и целостность", dependencies=[Depends(require_admin)])


class Checkpoint(BaseModel):
    format: Literal["durov-integrity-v1"]
    sequence: int = Field(ge=1)
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    genesis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@app.post("/verify")
def verify_chain(checkpoint: Checkpoint | None = None, db: Session = Depends(get_db)):
    return verify(db, checkpoint.model_dump() if checkpoint else None)


@app.get("/events")
def events(before: int | None = Query(None, ge=1), limit: int = Query(30, ge=1, le=100),
           entity: str | None = Query(None, max_length=128), db: Session = Depends(get_db)):
    query = select(IntegrityEvent)
    if before is not None:
        query = query.where(IntegrityEvent.sequence < before)
    if entity:
        query = query.where(IntegrityEvent.entity == entity)
    rows = db.scalars(query.order_by(IntegrityEvent.sequence.desc()).limit(limit)).all()
    items = []
    for row in rows:
        try:
            body = json.loads(row.payload)
        except ValueError:
            raise HTTPException(409, "Повреждена запись истории. Запустите проверку цепочки.")
        # Full business snapshots are deliberately not exported into a global
        # feed. This endpoint exposes provenance, not salaries/customer data.
        items.append({"sequence": row.sequence, "entity": row.entity, "entity_key": row.entity_key,
                      "action": row.action, "event_hash": row.event_hash,
                      "actor": body.get("actor"), "occurred_at": body.get("occurred_at"),
                      "changed_columns": body.get("changed_columns", []),
                      "references": body.get("references", [])})
    return {"items": items, "next_before": rows[-1].sequence if len(rows) == limit else None}


@app.get("/files/{file_id}")
def check_file(file_id: int, db: Session = Depends(get_db)):
    asset = db.get(FileAsset, file_id)
    if not asset:
        raise HTTPException(404, "Документ не найден")
    return {"file_id": asset.id, "state": file_integrity(asset), "sha256": asset.sha256,
            "size_bytes": asset.size_bytes, "document_truth_verified": False}
