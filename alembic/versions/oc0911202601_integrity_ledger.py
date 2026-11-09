"""Database-enforced, append-only hash chain for business changes.

No historic record is retroactively declared approved or documented.
The database owner can disable these controls: external checkpoints remain
necessary. Keep this migration self-contained for reproducible installations.
"""
from alembic import op
import json
import sqlalchemy as sa

revision = "oc0911202601"
down_revision = "ch0911202601"
branch_labels = depends_on = None

TABLES = (
    "users", "user_module_access", "file_assets", "clients", "client_notes",
    "crm_entities", "crm_events", "costing_settings", "costing_materials",
    "costing_items", "costing_estimates", "costing_revisions", "costing_audit",
    "costing_price_versions", "costing_order_links", "sales_orders", "sales_shipments",
    "sales_events", "supplier_orders", "supplies", "supply_lines", "suppliers",
    "supplier_price_items", "purchase_receipts", "purchase_returns", "warehouse_materials",
    "warehouse_operations", "stock_documents", "stock_movements", "stock_reservations",
    "stock_reservation_events", "productions", "production_blocks", "block_materials",
    "material_requests", "house_technical_cards", "local_recipes", "manufacture_runs",
    "organizations", "bank_accounts", "counterparties", "money_movements",
    "production_stage_templates", "template_blocks", "template_block_tasks", "template_block_materials",
)

SQL = r"""
CREATE FUNCTION public.integrity_reject_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    RAISE EXCEPTION 'Protected history cannot be changed or truncated' USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION public.integrity_append(body jsonb) RETURNS bigint
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    last_number bigint;
    last_hash text;
    serialised text;
BEGIN
    PERFORM pg_advisory_xact_lock(763924015092601::bigint);
    SELECT sequence, event_hash INTO last_number, last_hash
        FROM public.integrity_events ORDER BY sequence DESC LIMIT 1;
    last_number := coalesce(last_number, 0) + 1;
    last_hash := coalesce(last_hash, repeat('0', 64));
    serialised := (body || jsonb_build_object('sequence', last_number, 'version', 1))::text;
    INSERT INTO public.integrity_events(sequence, entity, entity_key, action, payload, previous_hash, event_hash)
    VALUES (last_number, body->>'entity', body->>'entity_key', body->>'action', serialised, last_hash,
        encode(sha256(convert_to(last_hash || E'\n' || serialised, 'UTF8')), 'hex'));
    RETURN last_number;
END;
$$;

CREATE FUNCTION public.integrity_capture() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    before_row jsonb;
    after_row jsonb;
    identity_row jsonb;
    key_data jsonb := '{}'::jsonb;
    links jsonb := '[]'::jsonb;
    changed jsonb;
    column_name text;
    relation jsonb;
    actor jsonb;
    omitted text[] := ARRAY['hashed_password', 'password', 'token', 'access_token', 'refresh_token',
                             'secret', 'api_key', 'path_on_disk'];
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN before_row := to_jsonb(OLD); END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN after_row := to_jsonb(NEW); END IF;
    IF TG_OP = 'UPDATE' AND before_row = after_row THEN RETURN NEW; END IF;
    identity_row := coalesce(after_row, before_row);
    FOR column_name IN SELECT jsonb_array_elements_text(TG_ARGV[0]::jsonb) LOOP
        key_data := key_data || jsonb_build_object(column_name, identity_row->column_name);
    END LOOP;
    FOR relation IN SELECT value FROM jsonb_array_elements(TG_ARGV[1]::jsonb) LOOP
        IF identity_row->(relation->>'column') IS NOT NULL
           AND identity_row->(relation->>'column') <> 'null'::jsonb THEN
            links := links || jsonb_build_array(relation || jsonb_build_object('value', identity_row->(relation->>'column')));
        END IF;
    END LOOP;
    SELECT coalesce(jsonb_agg(k ORDER BY k), '[]'::jsonb) INTO changed
      FROM jsonb_object_keys(coalesce(before_row, '{}'::jsonb) || coalesce(after_row, '{}'::jsonb)) AS names(k)
      WHERE before_row->k IS DISTINCT FROM after_row->k;
    actor := coalesce(nullif(current_setting('durov.actor', true), '')::jsonb,
                      '{"kind":"system_or_unattributed"}'::jsonb);
    PERFORM public.integrity_append(jsonb_build_object(
        'entity', TG_TABLE_NAME, 'entity_key', key_data::text, 'action', TG_OP,
        'occurred_at', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'actor', actor, 'before', before_row - omitted, 'after', after_row - omitted,
        'changed_columns', changed, 'references', links));
    RETURN coalesce(NEW, OLD);
END;
$$;

CREATE TRIGGER integrity_history_immutable BEFORE UPDATE OR DELETE ON public.integrity_events
FOR EACH ROW EXECUTE FUNCTION public.integrity_reject_mutation();
CREATE TRIGGER integrity_history_no_truncate BEFORE TRUNCATE ON public.integrity_events
FOR EACH STATEMENT EXECUTE FUNCTION public.integrity_reject_mutation();
REVOKE ALL ON FUNCTION public.integrity_append(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.integrity_capture() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.integrity_reject_mutation() FROM PUBLIC;
"""


def upgrade():
    op.add_column("file_assets", sa.Column("sha256", sa.String(64), nullable=True))
    op.add_column("file_assets", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.create_table("integrity_events",
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("entity", sa.String(128), nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_integrity_events_entity", "integrity_events", ["entity"])
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return  # Development SQLite must display protection as unavailable.
    op.execute(SQL)
    inspector = sa.inspect(connection)
    for name in TABLES:
        pk = inspector.get_pk_constraint(name)["constrained_columns"]
        if not pk:
            raise RuntimeError("Audited business table has no primary key: " + name)
        relations = []
        for fk in inspector.get_foreign_keys(name):
            for source, target in zip(fk["constrained_columns"], fk["referred_columns"]):
                relations.append({"column": source, "table": fk["referred_table"], "target_column": target})
        # Identifiers are the frozen allowlist above, arguments are JSON-escaped SQL literals.
        arguments = ", ".join("'" + json.dumps(value).replace("'", "''") + "'" for value in (pk, relations))
        op.execute(f'CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON public."{name}" '
                   f'FOR EACH ROW EXECUTE FUNCTION public.integrity_capture({arguments})')
        op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON public."{name}" '
                   'FOR EACH STATEMENT EXECUTE FUNCTION public.integrity_reject_mutation()')
    connection.execute(sa.text("SELECT public.integrity_append(CAST(:body AS jsonb))"), {"body": json.dumps({
        "entity": "ledger", "entity_key": "genesis", "action": "START",
        "actor": {"kind": "schema_migration", "revision": revision},
        "coverage": list(TABLES), "historical_documents_verified": False,
        "note": "History starts at this migration; prior documents and amounts are not verified by this record.",
    })})


def downgrade():
    # Removing an audit history silently would defeat the purpose of this migration.
    raise RuntimeError("Integrity history is retained. Restore application code without dropping this migration.")
