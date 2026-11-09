from datetime import datetime, timezone
import json
from fastapi import HTTPException
from sqlalchemy import update
from app.crm.models import CRMEntity, CRMEvent
from app.crm.schemas import ContactData, DealData, TaskData, PipelineData, StageData, InquiryData
from app.moysklad.models import SourceRecord
from app.users.models import User
from app.common.module_access import Module
from pydantic import ValidationError
from app.crm.sales_fields import merge_sales_patch, temperature_expression, validate_sales_fields
from app.crm.privacy import can_read_private, prepare_write, redact, visible_history

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
    if not archived:
        from app.crm.guard import ensure_followup, aware, settings
        from datetime import timedelta

        ensure_followup(
            db,
            row,
            key="website-task/" + str(request.id),
            responsible_id=client.manager_id,
            due_at=aware(request.created_at) + timedelta(hours=settings(db)["response_hours"]),
        )
    return row


_UNSET = object()


def out(row, *, source_fields=None, priority_override=_UNSET, user=None):
    from sqlalchemy.orm import object_session
    from app.crm.models import CRMTaskLink

    db = object_session(row)
    task_link = db.get(CRMTaskLink, row.id) if db and row.kind == "task" else None
    if source_fields is None:
        from app.crm.dates import source_date_expressions, source_date_fields

        values = db.query(*source_date_expressions(db, row.kind)).filter(CRMEntity.id == row.id).one() if db else (None, "unknown")
        source_fields = source_date_fields(*values)
    priority = priority_override
    if priority is _UNSET:
        from app.tasks.models import Task

        task = db.get(Task, task_link.task_id) if task_link else None
        priority = task.priority.value if task else None
    return {
        **source_fields,
        "priority": priority,
        "board_task_id": task_link.task_id if task_link else None,
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "archived": row.archived,
        "data": redact(row.payload, user),
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
        raise HTTPException(422, 'Проверьте формат полей карточки') from error
    uid = data.get("responsible_user_id")
    if uid is not None:
        user = db.get(User, uid)
        if not user or not user.is_active or not user.has_access(Module.CLIENTS):
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
    if kind == "inquiry" and data.get("is_spam") and data.get("accepted_deal_id"):
        raise HTTPException(422, "Принятую в работу заявку нельзя пометить как спам")
    if kind == "inquiry" and data.get("is_spam") and data.get("deal_id"):
        linked = db.get(CRMEntity, data["deal_id"])
        if linked and linked.kind == "deal" and not linked.payload.get("is_unsorted"):
            raise HTTPException(422, "Заявка уже связана с обычной сделкой; отметка спама недоступна")
    return validate_sales_fields(db, kind, data, previous)


def _effective_owner_expression(kind):
    """Match the record card: the linked commercial owner takes precedence."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import aliased

    direct_owner = CRMEntity.payload["responsible_user_id"].as_integer()
    if kind != "inquiry":
        return direct_owner
    linked_deal = aliased(CRMEntity)
    linked_id = func.coalesce(
        func.nullif(CRMEntity.payload["accepted_deal_id"].as_integer(), 0),
        CRMEntity.payload["deal_id"].as_integer(),
    )
    linked_owner = (
        select(linked_deal.payload["responsible_user_id"].as_integer())
        .where(linked_deal.kind == "deal", linked_deal.id == linked_id)
        .correlate(CRMEntity)
        .scalar_subquery()
    )
    return func.coalesce(linked_owner, direct_owner)


def _task_priority_expression():
    from sqlalchemy import select
    from app.crm.models import CRMTaskLink
    from app.tasks.models import Task

    return (select(Task.priority).join(CRMTaskLink, CRMTaskLink.task_id == Task.id)
            .where(CRMTaskLink.entity_id == CRMEntity.id).correlate(CRMEntity).scalar_subquery())


def _entries_query(db, kind, query="", archive="active", pipeline_id=None, stage_id=None, owner_id=None,
                   date_from=None, date_to=None, date_unknown=False, priority=None, spam="normal", temperature=None, user=None):
    """One filter contract for paginated registers and unpaginated board totals."""
    if kind not in SCHEMAS:
        raise HTTPException(422, "Неизвестный раздел продаж")
    from app.crm.dates import date_bounds, source_date_expressions

    start, end = date_bounds(date_from, date_to, date_unknown)
    if priority is not None and (kind != "task" or priority not in ("low", "medium", "high")):
        raise HTTPException(422, "Приоритет доступен только для задач: low, medium, high")
    if spam not in ("normal", "spam", "all"):
        raise HTTPException(422, "Неизвестный фильтр спама")
    stmt = db.query(CRMEntity).filter_by(kind=kind)
    if temperature is not None:
        if kind not in ('contact', 'deal', 'inquiry') or temperature not in ('hot', 'warm', 'cold', 'unknown'):
            raise HTTPException(422, 'Температура доступна для контактов, сделок и входящих заявок')
        value = temperature_expression(kind)
        stmt = stmt.filter(value.is_(None) if temperature == 'unknown' else value == temperature)
    if kind == "inquiry" and spam != "all":
        from sqlalchemy import func

        flagged = func.coalesce(CRMEntity.payload["is_spam"].as_boolean(), False)
        stmt = stmt.filter(flagged.is_(spam == "spam"))
        if spam == "spam":
            stmt = stmt.filter(func.coalesce(CRMEntity.payload["accepted_deal_id"].as_integer(), 0) == 0)
    if kind == "deal":
        from sqlalchemy import func, false

        stmt = stmt.filter(func.coalesce(CRMEntity.payload["is_unsorted"].as_boolean(), False).is_(False))
        if spam == "spam":
            stmt = stmt.filter(false())
    if archive != "all":
        stmt = stmt.filter(CRMEntity.archived.is_(archive == "archived"))
    if query:
        from sqlalchemy import cast, String, or_

        searchable = [CRMEntity.name]
        if can_read_private(user):
            searchable.append(cast(CRMEntity.payload, String))
        else:
            searchable += [CRMEntity.payload[key].as_string() for key in ('phone', 'email', 'contact_name', 'source_name')]
            searchable += [CRMEntity.payload['customer'][key].as_string() for key in ('phone_2', 'phone_3', 'city', 'first_name', 'last_name', 'middle_name')]
        stmt = stmt.filter(or_(*(func_lower(value).contains(query.casefold(), autoescape=True) for value in searchable)))
    if pipeline_id is not None:
        stmt = stmt.filter(CRMEntity.payload["pipeline_id"].as_integer() == pipeline_id)
    if stage_id is not None:
        stmt = stmt.filter(CRMEntity.payload["stage_id"].as_integer() == stage_id)
    if owner_id is not None:
        from sqlalchemy import func

        stmt = stmt.filter(func.coalesce(_effective_owner_expression(kind), 0) == owner_id)
    if date_unknown or start is not None or end is not None:
        epoch, _ = source_date_expressions(db, kind)
        if date_unknown:
            stmt = stmt.filter(epoch.is_(None))
        if start is not None:
            stmt = stmt.filter(epoch >= start)
        if end is not None:
            stmt = stmt.filter(epoch < end)
    if priority is not None:
        from app.tasks.models import TaskPriority

        stmt = stmt.filter(_task_priority_expression() == TaskPriority(priority))
    return stmt


def list_entries(db, kind, query="", archive="active", pipeline_id=None, stage_id=None, limit=50, offset=0, owner_id=None,
                 date_from=None, date_to=None, date_unknown=False, priority=None, spam="normal", temperature=None, user=None):
    from sqlalchemy import literal
    from app.crm.dates import source_date_expressions, source_date_fields

    stmt = _entries_query(db, kind, query, archive, pipeline_id, stage_id, owner_id,
                          date_from, date_to, date_unknown, priority, spam, temperature, user)
    epoch, origin = source_date_expressions(db, kind)
    order = [epoch.desc().nulls_last(), CRMEntity.id.desc()] if kind in ("deal", "inquiry") else [CRMEntity.id.desc()]
    priority_column = _task_priority_expression() if kind == "task" else literal(None)
    return {
        "total": stmt.count(),
        "items": [
            {**out(row, source_fields=source_date_fields(created, source_kind), priority_override=task_priority, user=user),
             "effective_owner_id": effective_owner or None}
            for row, effective_owner, created, source_kind, task_priority in stmt.add_columns(
                _effective_owner_expression(kind), epoch, origin, priority_column)
            .order_by(*order).offset(offset).limit(limit)
        ],
    }


def func_lower(value):
    from sqlalchemy import func

    return func.lower(value)


def kanban_summary(db, query="", archive="active", pipeline_id=None, owner_id=None,
                   date_from=None, date_to=None, date_unknown=False, spam="normal", temperature=None, user=None):
    """Commercial budgets of all canonical board cards, independent of page size.

    Pending inquiries represent their hidden unsorted deal once. Website inquiries
    without a pipeline belong to the default pre-sales New stage, as in the board.
    No internal economics or arbitrary CRM payload is returned to the caller.
    """
    from decimal import Decimal, InvalidOperation
    import re

    catalog = db.query(CRMEntity).filter(
        CRMEntity.kind.in_(["pipeline", "stage"]), CRMEntity.archived.is_(False)
    ).all()
    pipelines = [row for row in catalog if row.kind == "pipeline"]
    pipeline_ids = {row.id for row in pipelines}
    default_pipeline = next(
        (row.id for row in pipelines if row.name.strip().casefold() == "допродажная воронка"),
        pipelines[0].id if pipelines else None,
    )
    stages = sorted(
        (row for row in catalog if row.kind == "stage" and row.payload.get("pipeline_id") in pipeline_ids),
        key=lambda row: (row.payload.get("sort", 0), row.id),
    )
    first_stage = {}
    columns = {}
    for row in stages:
        pid = row.payload["pipeline_id"]
        if row.payload.get("outcome") == "open":
            first_stage.setdefault(pid, row.id)
        if pipeline_id is None or pipeline_id == pid:
            columns[(pid, row.id)] = {
                "pipeline_id": pid, "stage_id": row.id, "count": 0,
                "amounts": {}, "unpriced_count": 0,
            }

    date_filter = {"date_from": date_from, "date_to": date_to, "date_unknown": date_unknown, "spam": spam, "temperature": temperature, "user": user}
    deals = _entries_query(db, "deal", query, archive, owner_id=owner_id, **date_filter).all()
    regular_ids = {row.id for row in deals}
    inquiries = _entries_query(db, "inquiry", query, archive, owner_id=owner_id, **date_filter).all()
    linked_ids = {row.payload.get("deal_id") for row in inquiries if row.payload.get("deal_id")}
    linked_deals = {
        row.id: row for row in db.query(CRMEntity).filter(CRMEntity.kind == "deal", CRMEntity.id.in_(linked_ids))
    } if linked_ids else {}
    represented = set(regular_ids)
    unmapped = 0

    def add(pid, sid, payload):
        nonlocal unmapped
        if pipeline_id is not None and pid != pipeline_id:
            return
        column = columns.get((pid, sid))
        if column is None:
            unmapped += 1
            return
        column["count"] += 1
        currency = payload.get("currency") or "RUB"
        try:
            amount = Decimal(str(payload.get("amount", 0)))
        except (InvalidOperation, ValueError, TypeError):
            amount = Decimal(0)
        if not amount.is_finite() or amount <= 0 or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            column["unpriced_count"] += 1
            return
        column["amounts"][currency] = column["amounts"].get(currency, Decimal(0)) + amount

    for row in deals:
        add(row.payload.get("pipeline_id"), row.payload.get("stage_id"), row.payload)
    for row in inquiries:
        data = row.payload
        if data.get("accepted_deal_id"):
            continue
        linked = linked_deals.get(data.get("deal_id"))
        # A stale inquiry pointer must not double the existing deal's budget.
        if linked and linked.id in represented:
            continue
        if linked:
            represented.add(linked.id)
        pid = data.get("pipeline_id") if data.get("pipeline_id") is not None else default_pipeline
        add(pid, first_stage.get(pid), linked.payload if linked else {})
    for column in columns.values():
        column["amounts"] = {currency: float(value) for currency, value in sorted(column["amounts"].items())}
    return {"columns": list(columns.values()), "unmapped_count": unmapped}


def create(db, kind, entry, user):
    data = validate(db, kind, prepare_write(entry.data, user))
    if kind == "deal" and not data.get("responsible_user_id"):
        data["responsible_user_id"] = user.id
    if kind=='task' and data.get('entity_id') and not entry.archived:
        from app.crm.followup import step_owner
        parent=db.query(CRMEntity).filter_by(id=data['entity_id']).populate_existing().with_for_update(of=CRMEntity).one()
        if parent.kind in ('deal','inquiry','contact','company'):
            data['responsible_user_id']=step_owner(db,parent,user,data.get('responsible_user_id'))
    row = CRMEntity(kind=kind, name=entry.name.strip(), archived=entry.archived, payload=data)
    if not row.name:
        raise HTTPException(422, "Введите название")
    db.add(row)
    db.flush()
    if kind == "task":
        from app.crm.tasks import sync_to_board

        sync_to_board(db, row)
    if kind == "deal" and not row.archived and not data.get("is_unsorted"):
        from app.crm.guard import ensure_followup, close_terminal_followups

        ensure_followup(
            db, row, key="followup/deal/" + str(row.id), responsible_id=data.get("responsible_user_id")
        )
        close_terminal_followups(db, row, user)
    db.add(
        CRMEvent(
            entity_id=row.id, actor_id=user.id, action="created", details={"name": row.name, "data": data}
        )
    )
    db.commit()
    db.refresh(row)
    return out(row, user=user)


def patch(db, id, payload, user):
    row = get(db, id)
    if row.version != payload.version:
        raise HTTPException(409, "Запись уже изменена. Обновите карточку.")
    data = (
        validate(db, row.kind, merge_sales_patch(row.payload, prepare_write(payload.data, user, row.payload)), previous=row.payload)
        if payload.data is not None
        else row.payload
    )
    if row.kind == "inquiry" and bool(data.get("is_spam")) != bool(row.payload.get("is_spam")):
        if row.payload.get("accepted_deal_id") or row.archived:
            raise HTTPException(422, "Отметка спама доступна только для непринятой действующей заявки")
    values = {"payload": data, "version": row.version + 1, "updated_at": datetime.now(timezone.utc)}
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(422, "Введите название")
        values["name"] = payload.name.strip()
    if payload.archived is not None:
        values["archived"] = payload.archived
    before = {'name': row.name, 'data': row.payload, 'archived': row.archived}
    result = db.execute(
        update(CRMEntity)
        .where(CRMEntity.id == id, CRMEntity.version == payload.version)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Запись уже изменена. Обновите карточку.")
    if row.kind == "task":
        from app.crm.tasks import sync_to_board

        db.expire(row)
        db.refresh(row)
        sync_to_board(db, row)
    if row.kind == "deal":
        from app.crm.guard import close_terminal_followups

        db.expire(row)
        db.refresh(row)
        close_terminal_followups(db, row, user)
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
    return out(row, user=user)


def history(db, row, limit=100, offset=0, user=None):
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
    if entity and row.source_id and row.source_id.isdigit():
        imported = (
            db.query(SourceRecord)
            .filter(
                SourceRecord.entity_type == "amocrm/" + entity + "/notes",
                SourceRecord.payload["entity_id"].as_integer() == int(row.source_id),
            )
            .all()
        )
        for r in imported:
            if not can_read_private(user):
                from app.crm.import_access import public_history_entry
                safe = public_history_entry(db, r)
                if safe:
                    entries.append(safe)
                continue
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
                    "source_record_id": r.id,
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
    entries = visible_history(entries, row, user)
    entries.sort(key=lambda r: r["created_at"], reverse=True)
    return {"total": len(entries), "items": entries[offset : offset + limit]}


def add_note(db, row, text, user):
    if not text.strip():
        raise HTTPException(422, "Введите текст заметки")
    note = CRMEvent(entity_id=row.id, actor_id=user.id, action="note", details={"text": text.strip()})
    db.add(note)
    db.commit()
    return {"id": note.id, "text": text.strip()}


def files(db, query="", entity=None, offset=0, limit=50, user=None):
    stmt = db.query(SourceRecord).filter(
        SourceRecord.entity_type.in_(["amocrm/files", "amocrm/file_versions"])
    )
    if query and can_read_private(user):
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
        if not can_read_private(user):
            from app.crm.import_access import authorized_attachment
            try:
                _, public, _ = authorized_attachment(db, user, r)
            except HTTPException:
                continue
            if query and query.casefold() not in public['filename'].casefold():
                continue
            result = {'id': r.id, 'name': public['filename'], 'size': public['size'], 'archived': r.archived}
        else:
            result = {"id": r.id, "name": f["filename"], "size": f.get("size", 0), "archived": r.archived}
        seen.add(f["local_name"])
        results.append(result)
    return {"total": len(results), "items": results[offset : offset + limit]}


def accept_inquiry(db, row, entry, user):
    if row.kind != "inquiry":
        raise HTTPException(422, "Эта запись не является входящей заявкой")
    row = db.query(CRMEntity).filter_by(id=row.id).populate_existing().with_for_update().one()
    if row.payload.get("accepted_deal_id"):
        return out(get(db, row.payload["accepted_deal_id"], "deal"), user=user)
    if row.payload.get("is_spam"):
        raise HTTPException(422, "Сначала уберите отметку «Спам», затем примите заявку в работу")
    if row.archived:
        raise HTTPException(409, "Заявка находится в архиве")
    if not entry.name.strip():
        raise HTTPException(422, "Введите название сделки")
    old = row.payload
    deal = get(db, old["deal_id"], "deal") if old.get("deal_id") else None
    previous = deal.payload if deal else {}
    data = validate(
        db,
        "deal",
        {
            **previous,
            **({'referral': old['referral']} if old.get('referral') is not None else {}),
            **prepare_write(entry.data, user),
            **({'inquiry_customer_snapshot': old['customer']} if old.get('customer') is not None else {}),
            "is_unsorted": False,
            "client_id": old.get("client_id"),
            "client_request_id": old.get("client_request_id"),
        },
        previous={**previous, **({'referral': old['referral']} if old.get('referral') is not None else {})},
    )
    if not data.get("responsible_user_id"):
        data["responsible_user_id"] = user.id
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
                payload=ContactData(phone=phone, email=email, customer=old.get('customer')).model_dump(mode="json"),
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
    from app.crm.guard import accepted, close_terminal_followups

    accepted(db, row, deal, user)
    from app.telephony.service import accept_inquiry as transfer_inquiry_calls
    transfer_inquiry_calls(db, row, deal, user)
    from app.crm.chat_models import CRMChatLink
    for chat_link in db.query(CRMChatLink).filter_by(entity_id=row.id):
        chat_link.entity_id = deal.id
    close_terminal_followups(db, deal, user)
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
    return out(deal, user=user)
