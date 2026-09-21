"""Задача 0073-b: реальные дома каталожной модели (`GET
/catalog/{key}/productions`) и типовые АР/КР карточки — единственное
исключение из read-only витрины, правит только администратор."""

import io

from app.clients.models import Client
from app.common.files import FileAsset, FilePurpose
from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.house_models.models import HouseModelCard, HouseModelConfirmation, HouseModelKind
from app.production.models import Production


def _make_house_model_card(db, key="barn-dh96-test"):
    card = HouseModelCard(
        key=key,
        title="Барн DH 96 (тест)",
        kind=HouseModelKind.CATALOG,
        series="barn",
        confirmation=HouseModelConfirmation.CONFIRMED,
        confirmation_label="Подтверждено",
        source_note_path="test/fixture.md",
    )
    db.add(card)
    db.flush()
    return card


def _make_production_with_client(db, house_model_key, **client_fields):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    client = Client(
        cycle_id=cycle.id,
        full_name="Иван Клиентов",
        phone="+79160000000",
        email="ivan@example.com",
        final_price=5_000_000,
        installation_address="Московская обл., д. Тестово",
        house_model_key=house_model_key,
        **client_fields,
    )
    db.add(client)
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    return production


# --------------------------------------------------------- productions --


def test_productions_lists_only_houses_of_this_model_without_price_or_contacts(api, make_user, db):
    card = _make_house_model_card(db)
    other_card = _make_house_model_card(db, key="flat-dh43-test")
    production = _make_production_with_client(db, card.key)
    other_production = _make_production_with_client(db, other_card.key)
    db.commit()

    worker = api(make_user(Module.HOUSE_MODELS))
    resp = worker.get(f"/api/house-models/catalog/{card.key}/productions")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body) == 1
    row = body[0]
    assert row["production_id"] == production.id
    assert row["cycle_id"] == production.cycle_id
    assert row["house_index"] == 1
    assert f"№{production.cycle_id}" in row["client_display_name"]

    assert other_production.id not in [r["production_id"] for r in body]

    body_text = resp.text
    assert "5000000" not in body_text
    assert "ivan@example.com" not in body_text
    assert "Тестово" not in body_text
    assert "Иван Клиентов" not in body_text
    assert "+79160000000" not in body_text


def test_productions_empty_list_for_model_with_no_built_houses(api, make_user, db):
    card = _make_house_model_card(db)
    db.commit()

    worker = api(make_user(Module.HOUSE_MODELS))
    resp = worker.get(f"/api/house-models/catalog/{card.key}/productions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_productions_404_for_unknown_model_key(api, make_user, db):
    worker = api(make_user(Module.HOUSE_MODELS))
    resp = worker.get("/api/house-models/catalog/does-not-exist/productions")
    assert resp.status_code == 404


# ------------------------------------------------------ typical АР/КР --


def test_typical_documents_upload_and_patch_requires_admin(api, make_user, db):
    card = _make_house_model_card(db)
    db.commit()

    worker = api(make_user(Module.HOUSE_MODELS))
    upload_resp = worker.post(
        "/api/house-models/catalog/typical-ar-file",
        files={"file": ("ar.pdf", io.BytesIO(b"%PDF-fake"), "application/pdf")},
    )
    assert upload_resp.status_code == 403

    patch_resp = worker.patch(
        f"/api/house-models/catalog/{card.key}/typical-documents",
        json={"typical_ar_file_id": 1},
    )
    assert patch_resp.status_code == 403


def test_typical_documents_admin_can_upload_and_link(api, make_user, db):
    card = _make_house_model_card(db)
    db.commit()

    admin = api(make_user(admin=True))

    ar_resp = admin.post(
        "/api/house-models/catalog/typical-ar-file",
        files={"file": ("ar.pdf", io.BytesIO(b"%PDF-fake-ar"), "application/pdf")},
    )
    assert ar_resp.status_code == 201, ar_resp.text
    ar_asset = ar_resp.json()
    assert ar_asset["purpose"] == "typical_architectural_decisions"

    kr_resp = admin.post(
        "/api/house-models/catalog/typical-kr-file",
        files={"file": ("kr.pdf", io.BytesIO(b"%PDF-fake-kr"), "application/pdf")},
    )
    assert kr_resp.status_code == 201, kr_resp.text
    kr_asset = kr_resp.json()

    patch_resp = admin.patch(
        f"/api/house-models/catalog/{card.key}/typical-documents",
        json={"typical_ar_file_id": ar_asset["id"], "typical_kr_file_id": kr_asset["id"]},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    body = patch_resp.json()
    assert body["typical_ar"]["id"] == ar_asset["id"]
    assert body["typical_kr"]["id"] == kr_asset["id"]

    # confirmed on the read endpoint too, and every other field on the card
    # is untouched (still import-only) — spot-check title survived as-is.
    detail = admin.get(f"/api/house-models/catalog/{card.key}").json()
    assert detail["typical_ar"]["id"] == ar_asset["id"]
    assert detail["title"] == "Барн DH 96 (тест)"


def test_typical_documents_patch_rejects_file_of_wrong_purpose(api, make_user, db):
    card = _make_house_model_card(db)
    admin_user = make_user(admin=True)
    wrong_purpose_asset = FileAsset(
        filename="contract.pdf", content_type="application/pdf", path_on_disk="/tmp/contract.pdf",
        purpose=FilePurpose.CONTRACT, uploaded_by_id=admin_user.id,
    )
    db.add(wrong_purpose_asset)
    db.commit()

    admin = api(admin_user)
    resp = admin.patch(
        f"/api/house-models/catalog/{card.key}/typical-documents",
        json={"typical_ar_file_id": wrong_purpose_asset.id},
    )
    assert resp.status_code == 400


def test_typical_documents_patch_only_touches_fields_sent(api, make_user, db):
    card = _make_house_model_card(db)
    admin_user = make_user(admin=True)
    ar_asset = FileAsset(
        filename="ar.pdf", content_type="application/pdf", path_on_disk="/tmp/ar.pdf",
        purpose=FilePurpose.TYPICAL_ARCHITECTURAL_DECISIONS, uploaded_by_id=admin_user.id,
    )
    db.add(ar_asset)
    db.flush()
    card.typical_ar_file_id = ar_asset.id
    db.commit()

    admin = api(admin_user)
    # PATCH without typical_ar_file_id must not clear the already-set АР.
    resp = admin.patch(
        f"/api/house-models/catalog/{card.key}/typical-documents",
        json={"typical_kr_file_id": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["typical_ar"]["id"] == ar_asset.id
    assert body["typical_kr"] is None
