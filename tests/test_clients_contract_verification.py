"""Договор клиента: источник файла и отдельная отметка «проверен» (0084-i).

- загрузка договора — это «загружен, не проверен», а не проверка;
- выйти из «Ипотеки/Одобрения в банке» с загруженным после ввода проверки
  договором можно только после отметки;
- свой файл отметить нельзя, если есть другой сотрудник с правом правки
  документов; без заметки «что сверено» отметки нет;
- замена файла сбрасывает отметку;
- договор, сгенерированный Мариной, гейт не проходит ни при каком варианте;
- договор, приложенный до ввода проверки (флаг не выставлен), гейт
  пропускает — для него только предупреждение в карточке.
"""

import io

from app.ai.tools import _attach_generated_document
from app.clients import service as client_service
from app.clients.models import Client, ContractSource
from app.clients.schemas import ClientCreate
from app.common.module_access import AccessLevel, Module

# Фикстура `api` подменяет текущего пользователя глобально — последний
# вызов `api(user)` действует на все клиенты. Поэтому пользователь выбирается
# вызовом `api(...)` прямо перед запросом от его имени.

PDF = b"%PDF-1.4\n%test\n"


def _client_at_approval(db) -> Client:
    """Клиент на «Ипотеке/Одобрении в банке» со всеми документными полями,
    кроме договора и приложения, — их тесты прикладывают сами."""
    client = client_service.create_client(
        db, ClientCreate(full_name="Проверка Договора", phone="+70000000000", email="contract@example.com")
    )
    for _ in range(3):  # LEAD -> DISCUSSION -> SITE_VISIT -> APPROVAL
        client_service.transition_stage(db, client)
    client.order_type = "single"
    client.final_price = 2_000_000
    client.installation_address = "г. Тест, ул. Тест, 1"
    client.ar_file_id = 1
    client.kr_file_id = 1
    db.commit()
    return client


def _upload(api_client, client_id: int, name: str = "contract.pdf", content_type: str = "application/pdf"):
    return api_client.post(
        f"/api/clients/{client_id}/contract-file",
        files={
            "contract": (name, io.BytesIO(PDF), content_type),
            "appendix": ("appendix.pdf", io.BytesIO(PDF), "application/pdf"),
        },
    )


def _verify(api_client, client_id: int, document: str, note: str = "Стороны, сумма, график оплаты, модель дома"):
    return api_client.post(
        f"/api/clients/{client_id}/contract/verify", json={"document": document, "note": note}
    )


def test_upload_is_not_verification_and_blocks_the_stage(api, make_user, db):
    client = _client_at_approval(db)
    uploader = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    a = api(uploader)

    body = _upload(a, client.id).json()
    for doc in ("contract", "contract_appendix"):
        assert body[f"{doc}_source"] == "uploaded"
        assert body[f"{doc}_verification_required"] is True
        assert body[f"{doc}_verified_at"] is None
        assert body[f"{doc}_verified_by"] is None

    resp = a.post(f"/api/clients/{client.id}/transition")
    assert resp.status_code == 400
    assert "Договор не проверен" in resp.json()["detail"]


def test_other_user_verifies_and_stage_passes(api, make_user, db):
    client = _client_at_approval(db)
    uploader = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    reviewer = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    _upload(api(uploader), client.id)

    # Свой файл — нельзя: есть другой сотрудник с правом документов.
    assert _verify(api(uploader), client.id, "contract").status_code == 403

    b = api(reviewer)
    resp = _verify(b, client.id, "contract")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contract_verified_by"] == {"id": reviewer.id, "full_name": reviewer.full_name}
    assert body["contract_verified_at"] is not None
    assert body["contract_verification_note"] == "Стороны, сумма, график оплаты, модель дома"

    # Приложение ещё не проверено — гейт держит.
    resp = b.post(f"/api/clients/{client.id}/transition")
    assert resp.status_code == 400
    assert "Приложение к договору не проверено" in resp.json()["detail"]

    assert _verify(b, client.id, "contract_appendix").status_code == 200
    resp = b.post(f"/api/clients/{client.id}/transition")
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"] == "payment"


def test_note_is_required(api, make_user, db):
    client = _client_at_approval(db)
    uploader = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    reviewer = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    _upload(api(uploader), client.id)

    resp = _verify(api(reviewer), client.id, "contract", note="   ")
    assert resp.status_code == 400
    assert db.get(Client, client.id).contract_verified_at is None


def test_sole_document_editor_may_verify_own_upload(api, make_user, db):
    client = _client_at_approval(db)
    only = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    # Сотрудник только с просмотром права документов не имеет — не в счёт.
    make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    a = api(only)
    _upload(a, client.id)

    assert _verify(a, client.id, "contract").status_code == 200


def test_replacing_the_file_resets_verification(api, make_user, db):
    client = _client_at_approval(db)
    uploader = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    reviewer = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    _upload(api(uploader), client.id)
    b = api(reviewer)
    assert _verify(b, client.id, "contract").status_code == 200
    assert _verify(b, client.id, "contract_appendix").status_code == 200

    a = api(uploader)
    body = _upload(a, client.id, name="contract-v2.pdf").json()
    for doc in ("contract", "contract_appendix"):
        assert body[f"{doc}_verified_at"] is None
        assert body[f"{doc}_verified_by"] is None
        assert body[f"{doc}_verification_note"] is None
    assert a.post(f"/api/clients/{client.id}/transition").status_code == 400


def test_generated_contract_never_passes_the_gate(api, make_user, db):
    client = _client_at_approval(db)
    marina_user = make_user(Module.CLIENTS, Module.AI, level=AccessLevel.EDIT)
    reviewer = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    for document_type in ("contract", "contract_appendix"):
        _attach_generated_document(
            db, marina_user, client.id, document_type, f"{document_type}.txt", "Текст, написанный Мариной"
        )
    db.commit()
    stored = db.get(Client, client.id)
    assert stored.contract_source == ContractSource.GENERATED
    assert stored.contract_appendix_source == ContractSource.GENERATED

    b = api(reviewer)
    resp = _verify(b, client.id, "contract")
    assert resp.status_code == 400
    assert "сгенерирован Мариной" in resp.json()["detail"]

    # Даже если отметка каким-то путём стоит — сгенерированный не проходит.
    stored.contract_verified_by_id = reviewer.id
    stored.contract_verified_at = stored.created_at
    stored.contract_appendix_verified_by_id = reviewer.id
    stored.contract_appendix_verified_at = stored.created_at
    db.commit()
    resp = b.post(f"/api/clients/{client.id}/transition")
    assert resp.status_code == 400
    assert "сгенерирован Мариной" in resp.json()["detail"]


def test_legacy_contract_without_verification_is_only_a_warning(api, make_user, db):
    """Договор, приложенный до ввода проверки: миграция ставит ему
    source=uploaded, verification_required остаётся False — переход не
    блокируется, отметки проверки при этом нет."""
    client = _client_at_approval(db)
    client.contract_file_id = 1
    client.contract_appendix_file_id = 1
    client.contract_source = ContractSource.UPLOADED
    client.contract_appendix_source = ContractSource.UPLOADED
    db.commit()
    worker = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))

    resp = worker.post(f"/api/clients/{client.id}/transition")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contract_verification_required"] is False
    assert body["contract_verified_at"] is None
