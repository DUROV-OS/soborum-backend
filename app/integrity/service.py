import hashlib
import json

from sqlalchemy import func, select, text

from app.integrity.models import IntegrityEvent

ZERO_HASH = "0" * 64
EXPECTED_TABLES = frozenset("""
purchase_control_records purchase_control_documents
warehouse_inventory_counts warehouse_inventory_acts warehouse_control_documents warehouse_control_records
users user_module_access file_assets clients client_notes crm_entities crm_events
costing_settings costing_materials costing_items costing_estimates costing_revisions
costing_audit costing_price_versions costing_order_links sales_orders sales_shipments
sales_events supplier_orders supplies supply_lines suppliers supplier_price_items
purchase_receipts purchase_returns warehouse_materials warehouse_operations stock_documents
stock_movements stock_reservations stock_reservation_events productions production_blocks
block_materials material_requests house_technical_cards local_recipes manufacture_runs
organizations bank_accounts counterparties money_movements production_stage_templates
template_blocks template_block_tasks template_block_materials
costing_approval_policy costing_signoffs costing_overhead_plans costing_evidence_documents
payment_evidence_documents payment_evidence_versions payment_evidence_checks
actual_cost_documents actual_cost_records actual_cost_versions actual_cost_decisions actual_cost_closures
""".split())


def digest(previous_hash, payload):
    return hashlib.sha256((previous_hash + "\n" + payload).encode("utf-8")).hexdigest()


def protection(db):
    if db.bind.dialect.name != "postgresql":
        return {"state": "unavailable", "covered_tables": [], "missing_controls": ["postgresql_required"]}
    rows = db.execute(text("""
        SELECT c.relname, t.tgname, t.tgenabled, t.tgtype, p.proname
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
        WHERE n.nspname='public' AND (t.tgname LIKE 'integrity_%' OR t.tgname IN ('costing_version_immutable','payment_evidence_immutable','actual_cost_immutable','journey_events_immutable','warehouse_control_immutable','purchase_control_immutable')) AND NOT t.tgisinternal
    """)).all()
    installed = {(row[0], row[1]): row[2:] for row in rows}
    missing = []
    covered = []
    for table in sorted(EXPECTED_TABLES):
        valid = True
        for name, kind, function in (("integrity_capture_change", 29, "integrity_capture"),
                                     ("integrity_no_truncate", 34, "integrity_reject_mutation")):
            value = installed.get((table, name))
            if not value or value[0] not in {"O", "A"} or value[1:] != (kind, function):
                missing.append(table + "." + name)
                valid = False
        if valid:
            covered.append(table)
    for name, kind in (("integrity_history_immutable", 27), ("integrity_history_no_truncate", 34)):
        value = installed.get(("integrity_events", name))
        if not value or value[0] not in {"O", "A"} or value[1:] != (kind, "integrity_reject_mutation"):
            missing.append("integrity_events." + name)
    for table in ('purchase_control_records','purchase_control_documents','warehouse_inventory_counts','warehouse_inventory_acts','warehouse_control_documents','warehouse_control_records','sales_events','costing_signoffs','costing_overhead_plans','costing_evidence_documents','costing_revisions','payment_evidence_documents','payment_evidence_versions','payment_evidence_checks','actual_cost_documents','actual_cost_records','actual_cost_versions','actual_cost_decisions','actual_cost_closures'):
        control='purchase_control_immutable' if table.startswith('purchase_control_') else 'warehouse_control_immutable' if table.startswith('warehouse_control_') or table.startswith('warehouse_inventory_') else 'journey_events_immutable' if table == 'sales_events' else 'actual_cost_immutable' if table.startswith('actual_') else 'payment_evidence_immutable' if table.startswith('payment_') else 'costing_version_immutable'
        value=installed.get((table,control))
        if not value or value[0] not in {'O','A'} or value[1:] != (27,'integrity_reject_mutation'):
            missing.append(table+'.'+control)
    return {"state": "active" if not missing else "incomplete", "covered_tables": covered, "missing_controls": missing}


def verify_rows(rows, checkpoint=None):
    """Verify exact persisted UTF-8 bytes, including projection and sequence.

    A checkpoint must come from independently retained earlier output. Comparing
    only against the current database cannot detect a complete rewrite.
    """
    previous, number, genesis, anchor_seen = ZERO_HASH, 0, None, False
    for row in rows:
        try:
            body = json.loads(row.payload)
            valid = (row.sequence == number + 1 and row.previous_hash == previous
                     and digest(previous, row.payload) == row.event_hash
                     and body["sequence"] == row.sequence and body["version"] == 1
                     and body["entity"] == row.entity and body["entity_key"] == row.entity_key
                     and body["action"] == row.action)
            if number == 0:
                valid = valid and body["action"] == "START" and body["entity"] == "ledger"
                genesis = row.event_hash
            if not valid:
                return {"state": "broken", "checked_events": number, "problem_sequence": row.sequence}
        except (TypeError, ValueError, KeyError):
            return {"state": "broken", "checked_events": number, "problem_sequence": row.sequence}
        number, previous = row.sequence, row.event_hash
        if checkpoint and number == checkpoint["sequence"]:
            anchor_seen = previous == checkpoint["event_hash"] and genesis == checkpoint["genesis_hash"]
    if not number:
        return {"state": "empty", "checked_events": 0}
    if checkpoint and not anchor_seen:
        return {"state": "checkpoint_mismatch", "checked_events": number}
    return {
        "state": "consistent", "checked_events": number,
        "independent_checkpoint": "matches_supplied_checkpoint" if checkpoint else "not_provided",
        "checkpoint": {"format": "durov-integrity-v1", "sequence": number,
                       "event_hash": previous, "genesis_hash": genesis},
    }


def verify(db, checkpoint=None):
    controls = protection(db)
    # Fix the upper bound; concurrent appends must not turn a good chain into a
    # false failure or change the checkpoint while the verification is streaming.
    head = db.scalar(select(func.max(IntegrityEvent.sequence))) or 0
    rows = db.scalars(select(IntegrityEvent).where(IntegrityEvent.sequence <= head)
                      .order_by(IntegrityEvent.sequence).execution_options(yield_per=500))
    result = verify_rows(rows, checkpoint)
    return {**result, "protection": controls, "distributed_blockchain": False,
            "document_truth_verified": False, "historical_records_verified": False}
