from datetime import datetime, timezone
import json
from fastapi import HTTPException
from sqlalchemy import update
from app.crm.models import CRMEntity, CRMEvent
from app.crm.schemas import ContactData, DealData, TaskData, PipelineData, StageData, InquiryData
from app.moysklad.models import SourceRecord
from app.users.models import User
from pydantic import ValidationError

SCHEMAS = {
    "contact": ContactData,
    "company": ContactData,
    "deal": DealData,
    "task": TaskData,
    "pipeline": PipelineData,
    "stage": StageData,
    "inquiry": InquiryData,
}


def register_client_request(db, client, request, *, archived=False):
    """Idempotent local sales inbox entry; caller owns the transaction."""
    source_id = "website/" + str(request.id)
    row = db.query(CRMEntity).filter_by(kind="inquiry", source_id=source_id).first()
    if row is not None:
        return row
    row = CRMEntity(
        kind="inquiry",
        source_id=source_id,
        name=("Повторная заявка: " if request.is_repeat else "Заявка с сайта: ") + client.full_name,
        archived=archived,
        created_at=request.created_at,
        payload=InquiryData(
            source_name=request.source,
            contact_name=client.full_name,
            phone=client.phone or "",
            email=client.email or "",
            client_id=client.id,
            client_request_id=request.id,
        ).model_dump(mode="json"),
    )
    db.add(row)
    db.flush()
    return row


def out(row):
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "archived": row.archived,
        "data": row.payload,
        "version": row.version,
        "source_id": row.source_id,
        "updated_at": row.updated_at,
    }


def get(db, id, kind=None):
    row = db.get(CRMEntity, id)
    if row is None or (kind and row.kind != kind):
        raise HTTPException(404, "Запись не найдена")
    return row


def validate(db, kind, payload, previous=None):
    if kind not in SCHEMAS:
        raise HTTPException(422, "Неизвестный раздел продаж")
    try:
        data = SCHEMAS[kind].model_validate(payload).model_dump(mode="json")
    except ValidationError as error:
        raise HTTPException(422, str(error)) from error
    uid = data.get("responsible_user_id")
    if uid is not None:
        user = db.get(User, uid)
        if not user or not user.is_active:
            raise HTTPException(422, "Выберите действующего сотрудника")
    if kind in ("deal", "stage"):
        pipeline = get(db, data["pipeline_id"], "pipeline")
        if pipeline.archived:
            raise HTTPException(422, "Воронка находится в архиве")
    if kind == "deal":
        stage = get(db, data["stage_id"], "stage")
        if stage.archived or stage.payload["pipeline_id"] != data["pipeline_id"]:
            raise HTTPException(422, "Этап не принадлежит выбранной воронке")
        if (
            stage.payload.get("outcome") == "lost"
            and not data["loss_reason"].strip()
            and (previous is None or previous.get("stage_id") != data["stage_id"])
        ):
            raise HTTPException(422, "Укажите причину отказа")
        for id in data["contact_ids"]:
            get(db, id, "contact")
    if data.get("company_id"):
        get(db, data["company_id"], "company")
    if kind == "task" and data.get("entity_id"):
        get(db, data["entity_id"])
    if data.get("client_id"):
        from app.clients.models import Client

        if db.get(Client, data["client_id"]) is None:
            raise HTTPException(422, "Карточка клиента не найдена")
    if data.get("client_request_id"):
        from app.clients.models import ClientLeadRequest

        request = db.get(ClientLeadRequest, data["client_request_id"])
        if request is None or request.client_id != data.get("client_id"):
            raise HTTPException(422, "Заявка не принадлежит клиенту")
    return data


def list_entries(db, kind, query="", archive="active", pipeline_id=None, stage_id=None, limit=50, offset=0):
    if kind not in SCHEMAS:
        raise HTTPException(422, "Неизвестный раздел продаж")
    stmt = db.query(CRMEntity).filter_by(kind=kind)
    if kind == "deal":
        from sqlalchemy import func

        stmt = stmt.filter(func.coalesce(CRMEntity.payload["is_unsorted"].as_boolean(), False).is_(False))
    if archive != "all":
        stmt = stmt.filter(CRMEntity.archived.is_(archive == "archived"))
    if query:
        from sqlalchemy import cast, String, or_

        stmt = stmt.filter(
            or_(
                func_lower(CRMEntity.name).contains(query.casefold(), autoescape=True),
                func_lower(cast(CRMEntity.payload, String)).contains(query.casefold(), autoescape=True),
            )
        )
    if pipeline_id is not None:
        stmt = stmt.filter(CRMEntity.payload["pipeline_id"].as_integer() == pipeline_id)
    if stage_id is not None:
        stmt = stmt.filter(CRMEntity.payload["stage_id"].as_integer() == stage_id)
    return {
        "total": stmt.count(),
        "items": [out(r) for r in stmt.order_by(CRMEntity.id.desc()).offset(offset).limit(limit)],
    }


def func_lower(value):
    from sqlalchemy import func

    return func.lower(value)


def create(db, kind, entry, user):
    data = validate(db, kind, entry.data)
    row = CRMEntity(kind=kind, name=entry.name.strip(), archived=entry.archived, payload=data)
    if not row.name:
        raise HTTPException(422, "Введите название")
    db.add(row)
    db.flush()
    db.add(
        CRMEvent(
            entity_id=row.id, actor_id=user.id, action="created", details={"name": row.name, "data": data}
        )
    )
    db.commit()
    db.refresh(row)
    return out(row)


def patch(db, id, payload, user):
    row = get(db, id)
    if row.version != payload.version:
        raise HTTPException(409, "Запись уже изменена. Обновите карточку.")
    data = (
        validate(db, row.kind, {**row.payload, **payload.data}, previous=row.payload)
        if payload.data is not None
        else row.payload
    )
    values = {"payload": data, "version": row.version + 1, "updated_at": datetime.now(timezone.utc)}
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(422, "Введите название")
        values["name"] = payload.name.strip()
    if payload.archived is not None:
        values["archived"] = payload.archived
    before = out(row)
    result = db.execute(
        update(CRMEntity)
        .where(CRMEntity.id == id, CRMEntity.version == payload.version)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Запись уже изменена. Обновите карточку.")
    db.add(
        CRMEvent(
            entity_id=id,
            actor_id=user.id,
            action="updated",
            details={
                "before": {"name": before["name"], "data": before["data"], "archived": before["archived"]},
                "after": {k: v for k, v in values.items() if k != "updated_at"},
            },
        )
    )
    db.commit()
    db.expire(row)
    return out(row)


def history(db, row, limit=100, offset=0):
    local = db.query(CRMEvent).filter_by(entity_id=row.id).order_by(CRMEvent.id.desc()).all()
    entries = [
        {
            "id": "local-" + str(e.id),
            "created_at": e.created_at.isoformat(),
            "text": e.details.get("text")
            or ("Запись создана" if e.action == "created" else "Карточка изменена"),
            "details": e.details,
            "source": "soborum",
        }
        for e in local
    ]
    entity = {"deal": "leads", "contact": "contacts", "company": "companies"}.get(row.kind)
    if entity and row.source_id:
        imported = (
            db.query(SourceRecord)
            .filter(
                SourceRecord.entity_type == "amocrm/" + entity + "/notes",
                SourceRecord.payload["entity_id"].as_integer() == int(row.source_id),
            )
            .all()
        )
        for r in imported:
            p = r.payload
            params = p.get("params") or {}
            text = (
                params.get("text")
                or params.get("service")
                or params.get("link")
                or p.get("note_type")
                or "Запись истории"
            )
            if p.get("note_type") in ("call_in", "call_out"):
                text = (
                    ("Входящий звонок" if p["note_type"] == "call_in" else "Исходящий звонок")
                    + " · "
                    + str(params.get("phone") or "")
                    + " · "
                    + str(params.get("duration") or 0)
                    + " сек."
                )
                if params.get("call_result"):
                    text += "\n" + str(params["call_result"])
            elif p.get("note_type") == "amomail_message":
                text = (
                    "Письмо: "
                    + str(params.get("subject") or "")
                    + "\n"
                    + str(params.get("content_summary") or "")
                )
            entries.append(
                {
                    "id": "amo-" + r.source_id,
                    "created_at": datetime.fromtimestamp(p.get("created_at") or 0, timezone.utc).isoformat(),
                    "text": str(text),
                    "details": p,
                    "source": "amocrm",
                }
            )
    if row.payload.get("client_request_id"):
        from app.clients.models import ClientLeadRequest

        request = db.get(ClientLeadRequest, row.payload["client_request_id"])
        if request is not None and request.client_id == row.payload.get("client_id"):
            entries.append(
                {
                    "id": "website-" + str(request.id),
                    "created_at": request.created_at.isoformat(),
                    "text": "\n".join(
                        filter(
                            None,
                            [
                                request.source,
                                request.message,
                                "\n".join(f"{k}: {v}" for k, v in (request.utm or {}).items()),
                            ],
                        )
                    ),
                    "details": {"client_id": request.client_id, "is_repeat": request.is_repeat},
                    "source": "soborum",
                }
            )
    entries.sort(key=lambda r: r["created_at"], reverse=True)
    return {"total": len(entries), "items": entries[offset : offset + limit]}


def add_note(db, row, text, user):
    if not text.strip():
        raise HTTPException(422, "Введите текст заметки")
    note = CRMEvent(entity_id=row.id, actor_id=user.id, action="note", details={"text": text.strip()})
    db.add(note)
    db.commit()
    return {"id": note.id, "text": text.strip()}


def files(db, query="", entity=None, offset=0, limit=50):
    stmt = db.query(SourceRecord).filter(
        SourceRecord.entity_type.in_(["amocrm/files", "amocrm/file_versions"])
    )
    if query:
        stmt = stmt.filter(SourceRecord.search_text.contains(query.casefold(), autoescape=True))
    # File metadata is small (hundreds of records); Python also supports
    # SQLite tests without relying on PostgreSQL-only JSON containment.
    rows = stmt.order_by(SourceRecord.title).all()
    if entity:
        typ = {"deal": "leads", "contact": "contacts", "company": "companies"}.get(entity.kind)
        rows = [
            r
            for r in rows
            if typ
            and entity.source_id
            and any(
                str(e.get("id")) == entity.source_id and e.get("entity_type") == typ
                for e in r.payload.get("_linked_entities", [])
            )
        ]
    results = []
    seen = set()
    for r in rows:
        f = next(iter(r.payload.get("_downloaded_files", [])), None)
        if not f or f["local_name"] in seen:
            continue
        seen.add(f["local_name"])
        results.append({"id": r.id, "name": f["filename"], "size": f.get("size", 0), "archived": r.archived})
    return {"total": len(results), "items": results[offset : offset + limit]}


def accept_inquiry(db, row, entry, user):
    if row.kind != "inquiry":
        raise HTTPException(422, "Эта запись не является входящей заявкой")
    row = db.query(CRMEntity).filter_by(id=row.id).populate_existing().with_for_update().one()
    if row.payload.get("accepted_deal_id"):
        return out(get(db, row.payload["accepted_deal_id"], "deal"))
    if row.archived:
        raise HTTPException(409, "Заявка находится в архиве")
    if not entry.name.strip():
        raise HTTPException(422, "Введите название сделки")
    old = row.payload
    data = validate(
        db,
        "deal",
        {
            **entry.data,
            "is_unsorted": False,
            "client_id": old.get("client_id"),
            "client_request_id": old.get("client_request_id"),
        },
    )
    deal = get(db, old["deal_id"], "deal") if old.get("deal_id") else None
    if deal and not data.get("contact_ids"):
        data["contact_ids"] = deal.payload.get("contact_ids", [])
    if not data.get("contact_ids") and (old.get("phone") or old.get("email")):
        phone = old.get("phone", "")
        email = old.get("email", "")
        existing = db.query(CRMEntity).filter_by(kind="contact", archived=False).all()
        from app.clients.service import _phone_key, _email_key

        candidates = [
            r
            for r in existing
            if (_phone_key(phone) and _phone_key(r.payload.get("phone") or "") == _phone_key(phone))
            or (_email_key(email) and _email_key(r.payload.get("email") or "") == _email_key(email))
        ]
        contact = candidates[0] if len(candidates) == 1 else None
        if contact is None:
            contact = CRMEntity(
                kind="contact",
                name=old.get("contact_name") or entry.name,
                payload=ContactData(phone=phone, email=email).model_dump(mode="json"),
                archived=False,
            )
            db.add(contact)
            db.flush()
        data["contact_ids"] = [contact.id]
    if deal is None:
        deal = CRMEntity(kind="deal", name=entry.name.strip(), payload=data, archived=False)
        db.add(deal)
        db.flush()
    else:
        deal.name = entry.name.strip()
        deal.payload = data
        deal.version += 1
    row.payload = {**old, "accepted_deal_id": deal.id}
    row.archived = True
    row.version += 1
    if old.get("client_id"):
        from app.clients.models import Client
        from app.clients.service import take_lead

        client = db.get(Client, old["client_id"])
        if client is not None:
            take_lead(db, client)
    db.add(CRMEvent(entity_id=row.id, actor_id=user.id, action="accepted", details={"deal_id": deal.id}))
    db.add(
        CRMEvent(
            entity_id=deal.id, actor_id=user.id, action="inquiry_accepted", details={"inquiry_id": row.id}
        )
    )
    db.commit()
    return out(deal)
