from app.crm import service
from app.crm.schemas import Entry, Patch
from app.crm.models import CRMEntity
from app.crm.importer import import_export
from app.moysklad.models import SourceRecord
from app.common.module_access import Module, AccessLevel
from fastapi import HTTPException
import pytest


def pipeline(db, user):
    p = service.create(db, "pipeline", Entry(name="Продажи", data={}), user)
    open_ = service.create(db, "stage", Entry(name="Новый", data={"pipeline_id": p["id"]}), user)
    lost = service.create(
        db, "stage", Entry(name="Отказ", data={"pipeline_id": p["id"], "outcome": "lost"}), user
    )
    return p, open_, lost


def test_local_sales_workflow_and_conflict(db, make_user):
    user = make_user(admin=True)
    p, stage, lost = pipeline(db, user)
    c = service.create(db, "contact", Entry(name="Тестовый клиент", data={"phone": "+79990000000"}), user)
    deal = service.create(
        db,
        "deal",
        Entry(
            name="Дом",
            data={
                "pipeline_id": p["id"],
                "stage_id": stage["id"],
                "amount": 1234567.89,
                "contact_ids": [c["id"]],
            },
        ),
        user,
    )
    with pytest.raises(HTTPException) as e:
        service.patch(db, deal["id"], Patch(version=1, data={"stage_id": lost["id"]}), user)
    assert e.value.status_code == 422
    next_ = service.patch(
        db, deal["id"], Patch(version=1, data={"stage_id": lost["id"], "loss_reason": "Передумал"}), user
    )
    assert next_["data"]["amount"] == 1234567.89 and next_["data"]["loss_reason"] == "Передумал"
    with pytest.raises(HTTPException) as e:
        service.patch(db, deal["id"], Patch(version=1, name="Устаревшая правка"), user)
    assert e.value.status_code == 409
    service.add_note(db, service.get(db, deal["id"]), "Обсудили проект", user)
    assert service.history(db, service.get(db, deal["id"]))["total"] == 3
    task = service.create(
        db,
        "task",
        Entry(
            name="Перезвонить",
            data={
                "entity_id": deal["id"],
                "responsible_user_id": user.id,
                "due_at": "2026-11-01T12:00:00+03:00",
            },
        ),
        user,
    )
    task = service.patch(
        db,
        task["id"],
        Patch(version=task["version"], data={"completed": True, "result": "Созвонились"}),
        user,
    )
    assert task["data"]["completed"]


def test_permissions_validation_and_archive(db, make_user, api):
    owner = make_user(admin=True)
    p, s, l = pipeline(db, owner)
    view = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert view.get("/api/crm/catalog").status_code == 200
    assert view.post("/api/crm/entries/contact", json={"name": "Нельзя", "data": {}}).status_code == 403
    edit = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    assert edit.post("/api/crm/entries/pipeline", json={"name": "Нельзя", "data": {}}).status_code == 403
    c = edit.post("/api/crm/entries/contact", json={"name": "Клиент", "data": {"phone": "123"}}).json()
    assert (
        edit.patch("/api/crm/entries/" + str(c["id"]), json={"version": 1, "archived": True}).status_code
        == 403
    )
    assert (
        edit.post(
            "/api/crm/entries/deal",
            json={"name": "Ошибочная", "data": {"pipeline_id": p["id"], "stage_id": s["id"], "amount": -1}},
        ).status_code
        == 422
    )
    owner_api = api(owner)
    assert (
        owner_api.patch("/api/crm/entries/" + str(c["id"]), json={"version": 1, "archived": True}).status_code
        == 200
    )
    assert owner_api.get("/api/crm/entries?kind=contact").json()["total"] == 0
    assert owner_api.get("/api/crm/entries?kind=contact&archive=archived").json()["total"] == 1


def test_import_stages_deleted_history_and_native_retry(db, make_user):
    owner = make_user(admin=True)
    data = {
        "users": [],
        "leads/pipelines": [
            {
                "id": 10,
                "name": "Воронка",
                "_embedded": {"statuses": [{"id": 1, "name": "Новый"}, {"id": 143, "name": "Отказ"}]},
            }
        ],
        "contacts": [
            {
                "id": 20,
                "name": "Контакт",
                "custom_fields_values": [{"field_code": "PHONE", "values": [{"value": "123"}]}],
            }
        ],
        "leads": [
            {
                "id": 30,
                "name": "Дом",
                "pipeline_id": 10,
                "status_id": 1,
                "price": 1500000,
                "_embedded": {"contacts": [{"id": 20}]},
            }
        ],
        "contacts/deleted": [{"id": 21, "is_deleted": True}],
        "leads/notes": [
            {
                "id": 40,
                "entity_id": 30,
                "created_at": 100,
                "note_type": "common",
                "params": {"text": "История"},
            }
        ],
    }
    manifest = {"completed_at": "2026-10-28T00:00:00Z", "counts": {k: len(v) for k, v in data.items()}}
    import_export(db, data, manifest, owner.id)
    db.commit()
    deal = db.query(CRMEntity).filter_by(kind="deal").one()
    assert deal.payload["amount"] == 1500000
    assert len(deal.payload["contact_ids"]) == 1
    assert db.query(CRMEntity).filter_by(kind="contact", archived=True).count() == 1
    assert service.history(db, deal)["items"][0]["text"] == "История"
    deal.name = "Изменили у себя"
    db.commit()
    count = db.query(CRMEntity).count()
    import_export(db, data, manifest, owner.id)
    db.commit()
    assert db.query(CRMEntity).count() == count and deal.name == "Изменили у себя"
    assert db.query(SourceRecord).count() == sum(manifest["counts"].values())


def test_unsorted_accept_is_local_and_idempotent(db, make_user):
    user = make_user(admin=True)
    p, s, l = pipeline(db, user)
    inquiry = service.create(
        db,
        "inquiry",
        Entry(name="Звонок с сайта", data={"pipeline_id": p["id"], "phone": "123", "contact_name": "Клиент"}),
        user,
    )
    payload = Entry(
        name="Дом для клиента", data={"pipeline_id": p["id"], "stage_id": s["id"], "amount": 1200000}
    )
    deal = service.accept_inquiry(db, service.get(db, inquiry["id"]), payload, user)
    assert service.get(db, inquiry["id"]).archived
    assert service.list_entries(db, "deal")["total"] == 1
    assert len(deal["data"]["contact_ids"]) == 1
    repeat = service.accept_inquiry(db, service.get(db, inquiry["id"]), payload, user)
    assert repeat["id"] == deal["id"] and service.list_entries(db, "deal")["total"] == 1


def test_imported_files_are_scoped_and_path_is_confined(db, make_user, api, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.ai.live_tools import _read_imported_attachment

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    root = tmp_path / "moysklad" / "files"
    root.mkdir(parents=True)
    (root / "safe").write_text("Текст договора", encoding="utf-8")
    row = SourceRecord(
        entity_type="amocrm/files",
        source_id="file-1",
        title="Договор",
        archived=False,
        search_text="договор",
        payload={
            "_downloaded_files": [
                {"filename": "Договор.txt", "local_name": "safe", "content_type": "text/plain", "size": 30}
            ]
        },
    )
    db.add(row)
    db.commit()
    sales = make_user(Module.CLIENTS, Module.AI)
    assert _read_imported_attachment(db, sales, row.id)["text"] == "Текст договора"
    assert api(sales).get(f"/api/crm/files/{row.id}/download").status_code == 200
    warehouse = make_user(Module.WAREHOUSE, Module.AI)
    with pytest.raises(HTTPException) as e:
        _read_imported_attachment(db, warehouse, row.id)
    assert e.value.status_code == 403
    assert api(warehouse).get(f"/api/crm/files/{row.id}/download").status_code == 403
    row.payload = {"_downloaded_files": [{"filename": "outside.txt", "local_name": "../outside"}]}
    db.commit()
    with pytest.raises(HTTPException) as e:
        _read_imported_attachment(db, sales, row.id)
    assert e.value.status_code == 404
