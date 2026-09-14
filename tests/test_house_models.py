"""Типовые проекты домов (задача 0043-a): импорт идемпотентен, эндпоинты
read-only, гейт по Module.HOUSE_MODELS.

Раздел смонтирован под /api/house-models.
"""

from app.common.files import FileAsset
from app.common.module_access import Module
from app.house_models.data import HOUSE_MODEL_CARDS
from app.house_models.import_kb import ensure_house_models_seed
from app.house_models.models import HouseModelCard


def test_import_is_idempotent(db):
    created_first = ensure_house_models_seed(db)
    assert created_first == len(HOUSE_MODEL_CARDS)
    assert db.query(HouseModelCard).count() == len(HOUSE_MODEL_CARDS)

    created_second = ensure_house_models_seed(db)
    assert created_second == 0
    assert db.query(HouseModelCard).count() == len(HOUSE_MODEL_CARDS)


def test_catalog_lists_barn_and_flat_series_and_individual_projects(api, make_user, db):
    ensure_house_models_seed(db)
    client = api(make_user(Module.HOUSE_MODELS))

    resp = client.get("/api/house-models/catalog")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_series = {group["series"]: group["models"] for group in body["series"]}
    assert len(by_series["barn"]) == 8
    assert len(by_series["flat"]) == 7
    assert len(body["individual"]) == 6

    # sorted by footprint area within a series
    areas = [m["area_footprint_m2"] for m in by_series["barn"] if m["area_footprint_m2"] is not None]
    assert areas == sorted(areas)


def test_catalog_detail_barn_dh96_has_full_sections(api, make_user, db):
    ensure_house_models_seed(db)
    client = api(make_user(Module.HOUSE_MODELS))

    resp = client.get("/api/house-models/catalog/barn-dh96")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Барн DH 96"
    assert "Эко" in body["configurations_md"]
    assert body["economics_md"].count("|") > 0
    for version in ["A", "B", "C", "D/В"]:
        assert version in body["economics_md"]
    assert body["production_experience_md"].count("0003/DH-96") == 1
    assert "Q-03" in body["open_questions_md"]


def test_catalog_detail_barn_dh57_shows_absence_explicitly_not_blank(api, make_user, db):
    ensure_house_models_seed(db)
    client = api(make_user(Module.HOUSE_MODELS))

    resp = client.get("/api/house-models/catalog/barn-dh57")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["production_experience_md"] is not None
    assert "не найдено" in body["production_experience_md"]


def test_catalog_detail_missing_key_is_404(api, make_user, db):
    ensure_house_models_seed(db)
    client = api(make_user(Module.HOUSE_MODELS))

    resp = client.get("/api/house-models/catalog/does-not-exist")
    assert resp.status_code == 404


def test_catalog_requires_house_models_module(api, make_user, db):
    ensure_house_models_seed(db)
    client = api(make_user(Module.MARKETING))

    resp = client.get("/api/house-models/catalog")
    assert resp.status_code == 403


def test_planning_image_attached_only_where_it_exists_on_the_live_site(api, make_user, db):
    # save_bytes_file needs an uploader — an admin, like every other seed
    # that attaches files (see app.house_models.import_kb._attach_planning_image).
    make_user(admin=True)
    ensure_house_models_seed(db)
    client = api(make_user(Module.HOUSE_MODELS))

    dh96 = client.get("/api/house-models/catalog/barn-dh96").json()
    assert dh96["planning_image_id"] is not None

    # barn-dh83 has no page of its own on durov.house (see 0043-a) — no image, not a placeholder.
    dh83 = client.get("/api/house-models/catalog/barn-dh83").json()
    assert dh83["planning_image_id"] is None

    # individual projects aren't on the site catalog at all.
    dh59 = client.get("/api/house-models/catalog/barn-dh59").json()
    assert dh59["planning_image_id"] is None

    downloaded = client.get(f"/files/{dh96['planning_image_id']}")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] in ("image/jpeg", "image/jpeg; charset=utf-8")


def test_planning_image_import_is_idempotent(make_user, db):
    make_user(admin=True)
    ensure_house_models_seed(db)
    count_after_first = db.query(FileAsset).count()
    assert count_after_first == sum(1 for c in HOUSE_MODEL_CARDS if c["planning_image_asset"])

    ensure_house_models_seed(db)
    assert db.query(FileAsset).count() == count_after_first
