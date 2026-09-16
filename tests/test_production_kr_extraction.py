"""Постраничный разбор КР (0066-c): на реальном образце из `sources/` каждая
страница даёт непустой результат (текст и/или изображение), повторный запуск
после подмены файла заменяет старую запись, не дублирует её.
"""

from pathlib import Path

from app.clients.models import Client
from app.common.module_access import Module
from app.cycle.models import Cycle
from app.production.models import KrExtraction

SAMPLE_KR = Path(__file__).resolve().parents[2] / "sources" / "АР КР и Договор" / "КР_1 блок6.pdf"


def _make_client_with_kr(db, admin, path: Path = SAMPLE_KR):
    from app.common.files import FileAsset, FilePurpose

    cycle = Cycle()
    db.add(cycle)
    db.flush()
    client = Client(
        cycle_id=cycle.id, full_name="Клиент КР", phone="+79160000000", email="kr@example.com",
    )
    db.add(client)
    db.flush()
    asset = FileAsset(
        filename=path.name, content_type="application/pdf", path_on_disk=str(path),
        purpose=FilePurpose.CONSTRUCTIVE_DECISIONS, uploaded_by_id=admin.id,
    )
    db.add(asset)
    db.flush()
    client.kr_file_id = asset.id
    db.flush()
    return client


def test_extraction_on_real_sample_gives_nonempty_pages(api, make_user, db):
    assert SAMPLE_KR.is_file(), "реальный образец КР должен лежать в sources/"
    admin = make_user(admin=True)
    client = _make_client_with_kr(db, admin)
    db.commit()

    worker = api(make_user(Module.PRODUCTION))
    resp = worker.post(f"/api/production/kr-extraction/{client.id}")
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"] == client.id
    assert len(body["pages"]) > 0
    assert all(p["image_file_id"] for p in body["pages"])
    # По крайней мере часть страниц реального КР имеет текстовый слой.
    assert any(p["text"].strip() for p in body["pages"])


def test_get_extraction_returns_already_computed_result(api, make_user, db):
    admin = make_user(admin=True)
    client = _make_client_with_kr(db, admin)
    db.commit()

    worker = api(make_user(Module.PRODUCTION))
    missing = worker.get(f"/api/production/kr-extraction/{client.id}")
    assert missing.status_code == 404

    worker.post(f"/api/production/kr-extraction/{client.id}")
    resp = worker.get(f"/api/production/kr-extraction/{client.id}")
    assert resp.status_code == 200
    assert resp.json()["client_id"] == client.id


def test_rerun_replaces_previous_extraction_without_duplicating(api, make_user, db):
    admin = make_user(admin=True)
    client = _make_client_with_kr(db, admin)
    db.commit()

    worker = api(make_user(Module.PRODUCTION))
    first = worker.post(f"/api/production/kr-extraction/{client.id}")
    second = worker.post(f"/api/production/kr-extraction/{client.id}")
    assert first.status_code == 201 and second.status_code == 201

    rows = db.query(KrExtraction).filter(KrExtraction.client_id == client.id).all()
    assert len(rows) == 1


def test_extraction_requires_kr_file(api, make_user, db):
    cycle = Cycle()
    db.add(cycle)
    db.flush()
    client = Client(cycle_id=cycle.id, full_name="Без КР", phone="+79160000001", email="no-kr@example.com")
    db.add(client)
    db.commit()

    worker = api(make_user(Module.PRODUCTION))
    resp = worker.post(f"/api/production/kr-extraction/{client.id}")
    assert resp.status_code == 400
