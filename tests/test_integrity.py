import io
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import UploadFile

from app.common.files import FileAsset, FilePurpose, file_integrity, save_upload_file
from app.common.module_access import Module
from app.integrity.service import ZERO_HASH, digest, verify_rows


def chain():
    previous, rows = ZERO_HASH, []
    for number in range(1, 4):
        entity, action = ("ledger", "START") if number == 1 else ("money_movements", "UPDATE")
        body = {"sequence": number, "version": 1, "entity": entity, "entity_key": str(number), "action": action,
                "after": {"amount": "351.29"}}
        payload = json.dumps(body, ensure_ascii=False)
        event_hash = digest(previous, payload)
        rows.append(SimpleNamespace(sequence=number, entity=entity, entity_key=str(number), action=action,
                                    payload=payload, previous_hash=previous, event_hash=event_hash))
        previous = event_hash
    return rows


def test_integrity_migration_follows_the_current_private_schema():
    from app.core.readiness import expected_heads
    assert expected_heads() == frozenset({"pe0911202601"})


def test_checks_payload_chain_order_projection_and_checkpoint():
    rows = chain()
    checkpoint = verify_rows(rows)["checkpoint"]
    assert verify_rows(rows, checkpoint)["independent_checkpoint"] == "matches_supplied_checkpoint"
    assert verify_rows(rows[:2], checkpoint)["state"] == "checkpoint_mismatch"
    assert verify_rows(rows[1:])["state"] == "broken"
    rows[1].payload = rows[1].payload.replace("351.29", "0.00")
    assert verify_rows(rows)["problem_sequence"] == 2
    rows = chain()
    rows[1].entity = "sales_orders"
    assert verify_rows(rows)["state"] == "broken"


def test_rewritten_chain_is_detected_by_independently_saved_checkpoint():
    rows = chain()
    checkpoint = verify_rows(rows)["checkpoint"]
    rows[1].payload = rows[1].payload.replace("351.29", "0.00")
    previous = ZERO_HASH
    for row in rows:
        row.previous_hash = previous
        row.event_hash = digest(previous, row.payload)
        previous = row.event_hash
    assert verify_rows(rows)["state"] == "consistent"  # Honest scope of a centralised ledger.
    assert verify_rows(rows, checkpoint)["state"] == "checkpoint_mismatch"


def test_uploaded_bytes_are_hashed_and_tampering_blocks_download(db, make_user, api, tmp_path, monkeypatch):
    from app.common import files
    monkeypatch.setattr(files.settings, "storage_dir", str(tmp_path))
    user = make_user(Module.ACCOUNTING)
    content = b"document scan original"
    asset = save_upload_file(db, UploadFile(filename="../../escape.pdf", file=io.BytesIO(content)),
                             FilePurpose.MONEY_MOVEMENT_DOCUMENT, user)
    db.commit()
    assert Path(asset.path_on_disk).parent == tmp_path
    assert asset.size_bytes == len(content)
    assert file_integrity(asset) == "matches"
    assert api(user).get(f"/files/{asset.id}").status_code == 200
    Path(asset.path_on_disk).write_bytes(b"altered document")
    assert file_integrity(asset) == "mismatch"
    assert api(user).get(f"/files/{asset.id}").status_code == 409


def test_legacy_file_is_not_retroactively_verified_and_private_purpose_cannot_leak(db, make_user, api, tmp_path):
    user = make_user(admin=True)
    path = tmp_path / "original.txt"
    path.write_text("original")
    asset = FileAsset(filename="original.txt", content_type="text/plain", path_on_disk=str(path),
                      purpose=FilePurpose.FEEDBACK_ATTACHMENT, uploaded_by_id=user.id)
    db.add(asset); db.commit()
    assert file_integrity(asset) == "not_recorded"
    assert api(user).get(f"/files/{asset.id}").status_code == 403
    assert api(user).get(f"/api/integrity/files/{asset.id}").json()["state"] == "not_recorded"


def test_audit_api_admin_only_and_sqlite_never_claims_protection(db, make_user, api):
    worker, admin = make_user(Module.ACCOUNTING), make_user(admin=True)
    for path in ("/api/integrity/events", "/api/integrity/files/1"):
        assert api(worker).get(path).status_code == 403
    assert api(worker).post("/api/integrity/verify").status_code == 403
    result = api(admin).post("/api/integrity/verify").json()
    assert result["state"] == "empty" and result["protection"]["state"] == "unavailable"
    assert not result["distributed_blockchain"] and not result["document_truth_verified"]
