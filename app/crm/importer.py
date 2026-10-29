"""Read amoCRM exports into a local, editable CRM without creating user accounts."""

from datetime import datetime, timezone
import hashlib, json
from sqlalchemy import insert
from app.crm.models import CRMEntity
from app.moysklad.models import SourceRecord, ImportRun
from app.users.models import User, UserRole


def import_export(db, data, manifest, owner_id):
    owner = db.get(User, owner_id)
    if not owner or not owner.is_active or owner.role != UserRole.ADMIN:
        raise ValueError("Active owner required")
    for typ, count in manifest["counts"].items():
        if len(data.get(typ, [])) != count:
            raise ValueError("Incomplete amoCRM export: " + typ)
    now = datetime.now(timezone.utc)
    source_count = 0
    created = {}
    seen_keys = set()
    duplicates = 0
    existing = {
        (r.entity_type, r.source_id): r
        for r in db.query(SourceRecord).filter(SourceRecord.entity_type.startswith("amocrm/"))
    }
    pending = []

    def clean(obj):
        if isinstance(obj, dict):
            return {
                k: clean(v)
                for k, v in obj.items()
                if k.casefold()
                not in {"password", "access_token", "refresh_token", "client_secret", "secret", "token"}
            }
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj

    for typ, items in data.items():
        for original in items:
            raw = clean(original)
            sid = str(
                raw.get("id")
                or raw.get("uid")
                or raw.get("uuid")
                or hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
            )
            key = ("amocrm/" + typ, sid)
            if key in seen_keys:
                duplicates += 1
                continue
            seen_keys.add(key)
            fields = {
                "entity_type": key[0],
                "source_id": sid,
                "title": str(
                    raw.get("name") or raw.get("text") or raw.get("params", {}).get("text") or typ + " " + sid
                )[:1000],
                "archived": bool(
                    raw.get("is_deleted")
                    or raw.get("is_trashed")
                    or raw.get("archived")
                    or raw.get("is_archived")
                    or raw.get("is_archive")
                ),
                "state": None,
                "search_text": json.dumps(raw, ensure_ascii=False).casefold(),
                "payload": raw,
                "imported_at": now,
            }
            row = existing.get(key)
            if row:
                for k, v in fields.items():
                    setattr(row, k, v)
            else:
                pending.append(fields)
            source_count += 1
            if len(pending) >= 500:
                db.execute(insert(SourceRecord), pending)
                pending = []
    if pending:
        db.execute(insert(SourceRecord), pending)
    native = {(r.kind, r.source_id): r for r in db.query(CRMEntity)}

    def add(kind, sid, name, payload, archived=False):
        key = (kind, str(sid))
        if key in native:
            return native[key]
        row = CRMEntity(
            kind=kind,
            source_id=str(sid),
            name=str(name or kind + " " + str(sid))[:1000],
            archived=archived,
            payload=payload,
            version=1,
        )
        db.add(row)
        db.flush()
        native[key] = row
        created[kind] = created.get(kind, 0) + 1
        return row

    users = {u.email.casefold(): u.id for u in db.query(User).filter_by(is_active=True)}
    user_map = {u["id"]: users.get(u.get("email", "").casefold()) for u in data.get("users", [])}

    def responsible(raw):
        return user_map.get(raw.get("responsible_user_id"))

    def field(raw, code):
        for f in raw.get("custom_fields_values") or []:
            if f.get("field_code") == code:
                return next((str(v["value"]) for v in f.get("values", []) if v.get("value")), "")
        return ""

    def archived(raw):
        return bool(
            raw.get("is_deleted") or raw.get("is_archived") or raw.get("is_archive") or raw.get("archived")
        )

    pipelines = {}
    stages = {}
    companies = {}
    contacts = {}
    deals = {}
    unsorted_deals = {
        r["id"] for raw in data.get("leads/unsorted", []) for r in raw.get("_embedded", {}).get("leads", [])
    }
    for raw in data.get("leads/pipelines", []):
        p = add("pipeline", raw["id"], raw["name"], {"sort": raw.get("sort", 0)}, archived(raw))
        pipelines[raw["id"]] = p.id
        for s in raw.get("_embedded", {}).get("statuses", []):
            sid = str(raw["id"]) + "/" + str(s["id"])
            outcome = "won" if s["id"] == 142 else "lost" if s["id"] == 143 else "open"
            stage = add(
                "stage",
                sid,
                s["name"],
                {
                    "pipeline_id": p.id,
                    "sort": s.get("sort", 0),
                    "color": s.get("color") or "#dbeafe",
                    "outcome": outcome,
                },
                archived(s),
            )
            stages[(raw["id"], s["id"])] = stage.id
    for raw in data.get("companies", []):
        row = add(
            "company",
            raw["id"],
            raw.get("name"),
            {
                "phone": field(raw, "PHONE"),
                "email": field(raw, "EMAIL"),
                "company_id": None,
                "responsible_user_id": responsible(raw),
                "custom_fields": raw.get("custom_fields_values") or [],
            },
            archived(raw),
        )
        companies[raw["id"]] = row.id
    for raw in data.get("contacts", []):
        refs = raw.get("_embedded", {}).get("companies", [])
        company = companies.get(refs[0]["id"]) if refs else None
        row = add(
            "contact",
            raw["id"],
            raw.get("name"),
            {
                "phone": field(raw, "PHONE"),
                "email": field(raw, "EMAIL"),
                "company_id": company,
                "responsible_user_id": responsible(raw),
                "custom_fields": raw.get("custom_fields_values") or [],
                "is_unsorted": bool(raw.get("is_unsorted")),
            },
            archived(raw),
        )
        contacts[raw["id"]] = row.id
    for raw in data.get("leads", []):
        pid = raw.get("pipeline_id")
        sid = raw.get("status_id")
        if pid not in pipelines:
            p = add("pipeline", pid, "Воронка " + str(pid), {"sort": 0})
            pipelines[pid] = p.id
        if (pid, sid) not in stages:
            stage = add(
                "stage",
                str(pid) + "/" + str(sid),
                "Этап " + str(sid),
                {
                    "pipeline_id": pipelines[pid],
                    "sort": 0,
                    "color": "#dbeafe",
                    "outcome": "won" if sid == 142 else "lost" if sid == 143 else "open",
                },
            )
            stages[(pid, sid)] = stage.id
        embedded = raw.get("_embedded", {})
        cr = embedded.get("companies") or []
        loss = embedded.get("loss_reason") or []
        if isinstance(loss, list):
            loss = loss[0] if loss else {}
        payload = {
            "pipeline_id": pipelines[pid],
            "stage_id": stages[(pid, sid)],
            "amount": raw.get("price", 0),
            "currency": "RUB",
            "contact_ids": [contacts[c["id"]] for c in embedded.get("contacts", []) if c["id"] in contacts],
            "company_id": companies.get(cr[0]["id"]) if cr else None,
            "responsible_user_id": responsible(raw),
            "loss_reason": loss.get("name", ""),
            "custom_fields": raw.get("custom_fields_values") or [],
            "is_unsorted": raw["id"] in unsorted_deals or raw.get("status_id") == 1,
        }
        row = add("deal", raw["id"], raw.get("name"), payload, archived(raw))
        deals[raw["id"]] = row.id
    for raw in data.get("leads/unsorted", []):
        embedded = raw.get("_embedded", {})
        lead_refs = embedded.get("leads") or []
        contact_refs = embedded.get("contacts") or []
        contact = native.get(("contact", str(contact_refs[0]["id"]))) if contact_refs else None
        meta = raw.get("metadata") or {}
        add(
            "inquiry",
            raw["uid"],
            raw.get("source_name") or "Входящая заявка",
            {
                "pipeline_id": pipelines.get(raw.get("pipeline_id")),
                "source_name": raw.get("source_name") or "",
                "contact_name": contact.name if contact else "",
                "phone": (
                    str(contact.payload.get("phone") or meta.get("phone") or "")[:100]
                    if contact
                    else str(meta.get("phone") or "")[:100]
                ),
                "email": str(contact.payload.get("email") or "")[:255] if contact else "",
                "deal_id": deals.get(lead_refs[0]["id"]) if lead_refs else None,
                "accepted_deal_id": None,
                "reason": "",
            },
            archived(raw),
        )
    mapping = {"leads": deals, "contacts": contacts, "companies": companies}
    for raw in data.get("tasks", []):
        due = raw.get("complete_till")
        entity = mapping.get(raw.get("entity_type"), {}).get(raw.get("entity_id"))
        result = raw.get("result") or {}
        if not isinstance(result, dict):
            result = {"text": str(result)}
        add(
            "task",
            raw["id"],
            raw.get("text") or "Задача",
            {
                "entity_id": entity,
                "responsible_user_id": responsible(raw),
                "due_at": datetime.fromtimestamp(due, timezone.utc).isoformat() if due else None,
                "completed": bool(raw.get("is_completed")),
                "result": result.get("text", ""),
                "task_type": str(raw.get("task_type_id", "Задача")),
            },
            archived(raw),
        )
    # Tombstones stay archived; they must not re-enter the working pipelines.
    for typ, kind in [("leads", "deal"), ("contacts", "contact"), ("companies", "company")]:
        for raw in data.get(typ + "/deleted", []):
            row = native.get((kind, str(raw["id"])))
            if row:
                row.archived = True
            else:
                add(
                    kind, raw["id"], raw.get("name") or "Удалённая запись amoCRM #" + str(raw["id"]), {}, True
                )
    summary = {
        "source": "amocrm",
        "source_completed_at": manifest.get("completed_at"),
        "counts": manifest["counts"],
        "source_records": source_count,
        "duplicates_suppressed": duplicates,
        "native_created": created,
        "export_errors": manifest.get("errors", []),
    }
    db.add(ImportRun(summary=summary))
    db.flush()
    return summary


def main():
    import argparse
    from pathlib import Path
    from app.db import import_all_models
    from app.db.session import SessionLocal

    p = argparse.ArgumentParser()
    p.add_argument("directory")
    p.add_argument("--owner-id", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    root = Path(a.directory)
    with SessionLocal() as db:
        result = import_export(
            db,
            json.loads((root / "entities.json").read_text()),
            json.loads((root / "manifest.json").read_text()),
            a.owner_id,
        )
        if a.dry_run:
            db.rollback()
        else:
            db.commit()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
