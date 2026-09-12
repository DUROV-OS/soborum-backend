"""Клиент — выбор дома из каталога вместо ориентировочных цены/площади (0044):

- «Обсуждение» ничего не требует и не блокирует (раздел «Проектная
  информация» убран целиком);
- `order_type` обязателен для перехода «Согласование» -> «Оплата»,
  `house_model_key` — нет;
- `houses_count` редактируется в любой момент, даже после блокировки
  остальных документных данных;
- `ClientOut` не содержит удалённых полей и отдаёт привязанную модель
  каталога (`house_models`, 0043-a), если она указана.
"""

from app.clients import service as client_service
from app.clients.models import Client
from app.clients.schemas import ClientCreate, ClientDocumentsUpdate
from app.common.module_access import Module
from app.house_models.import_kb import ensure_house_models_seed


def _make_client(db, name="Клиент"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def test_discussion_to_approval_requires_nothing(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    resp = worker.post(f"/api/clients/{client.id}/transition")  # LEAD -> DISCUSSION
    assert resp.status_code == 200, resp.text
    resp = worker.post(f"/api/clients/{client.id}/transition")  # DISCUSSION -> APPROVAL
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"] == "approval"


def test_order_type_required_to_leave_approval_house_model_key_is_not(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))
    worker.post(f"/api/clients/{client.id}/transition")
    worker.post(f"/api/clients/{client.id}/transition")

    worker.patch(
        f"/api/clients/{client.id}/documents",
        json={"final_price": 2_000_000, "installation_address": "адрес"},
    )
    client_from_db = db.get(Client, client.id)
    client_from_db.contract_file_id = 1
    client_from_db.house_project_file_id = 1
    db.commit()

    missing_order_type = worker.post(f"/api/clients/{client.id}/transition")
    assert missing_order_type.status_code == 400
    assert "order_type" in missing_order_type.json()["detail"]

    ok = worker.patch(f"/api/clients/{client.id}/documents", json={"order_type": "single"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["house_model_key"] is None  # never required

    advanced = worker.post(f"/api/clients/{client.id}/transition")
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["stage"] == "payment"


def test_houses_count_editable_after_documents_locked(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))
    worker.post(f"/api/clients/{client.id}/transition")
    worker.post(f"/api/clients/{client.id}/transition")

    client_from_db = db.get(Client, client.id)
    client_from_db.contract_file_id = 1
    client_from_db.house_project_file_id = 1
    db.commit()
    worker.patch(
        f"/api/clients/{client.id}/documents",
        json={"order_type": "multiple", "final_price": 2_000_000, "installation_address": "адрес"},
    )
    worker.patch(f"/api/clients/{client.id}/houses-count", json={"houses_count": 3})
    locked = worker.post(f"/api/clients/{client.id}/transition")
    assert locked.status_code == 200, locked.text

    blocked = worker.patch(f"/api/clients/{client.id}/documents", json={"final_price": 3_000_000})
    assert blocked.status_code == 400

    still_open = worker.patch(f"/api/clients/{client.id}/houses-count", json={"houses_count": 4})
    assert still_open.status_code == 200, still_open.text
    assert still_open.json()["houses_count"] == 4


def test_houses_count_validates_against_order_type(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))
    worker.post(f"/api/clients/{client.id}/transition")
    worker.post(f"/api/clients/{client.id}/transition")
    worker.patch(f"/api/clients/{client.id}/documents", json={"order_type": "single"})

    resp = worker.patch(f"/api/clients/{client.id}/houses-count", json={"houses_count": 2})
    assert resp.status_code == 400
    assert "множественного заказа" in resp.json()["detail"]


def test_client_out_has_no_removed_fields_and_links_real_house_model(api, make_user, db):
    ensure_house_models_seed(db)
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))
    worker.post(f"/api/clients/{client.id}/transition")
    worker.post(f"/api/clients/{client.id}/transition")

    resp = worker.patch(f"/api/clients/{client.id}/documents", json={"house_model_key": "barn-dh96"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for removed in ["wishes_description", "estimated_price", "house_area", "layout_notes", "project_locked_at"]:
        assert removed not in body
    assert body["house_model_key"] == "barn-dh96"
    assert body["house_model"]["title"] == "Барн DH 96"
