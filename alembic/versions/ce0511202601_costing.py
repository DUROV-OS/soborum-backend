"""Versioned cost estimates, shared warehouse links and commercial price lists."""
from alembic import op
import sqlalchemy as sa

revision = "ce0511202601"
down_revision = "f4a9b58d732c"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE module ADD VALUE IF NOT EXISTS 'COSTING'")
    op.create_table("costing_settings", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("version", sa.Integer(), nullable=False), sa.Column("content", sa.JSON(), nullable=False))
    op.create_table("costing_materials", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("warehouse_material_id", sa.Integer(), sa.ForeignKey("warehouse_materials.id"), unique=True),
        sa.Column("name", sa.String(255), nullable=False), sa.Column("category", sa.String(120), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False), sa.Column("price", sa.Numeric(20, 8)),
        sa.Column("price_source", sa.String(1000), nullable=False), sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False), sa.Column("version", sa.Integer(), nullable=False))
    op.create_table("costing_items", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False), sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("content", sa.JSON(), nullable=False))
    op.create_index("ix_costing_items_kind", "costing_items", ["kind"])
    op.create_table("costing_estimates", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id")), sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False), sa.Column("number", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for name in ("client_id", "actor_id"):
        op.create_index("ix_costing_estimates_" + name, "costing_estimates", [name])
    op.create_table("costing_revisions", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("estimate_id", sa.Integer(), sa.ForeignKey("costing_estimates.id"), nullable=False), sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False), sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("request_id", sa.String(36), unique=True, nullable=False), sa.Column("request_data", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("estimate_id", "number", name="uq_costing_revision"))
    op.create_index("ix_costing_revisions_estimate_id", "costing_revisions", ["estimate_id"])
    op.create_table("costing_audit", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("entity", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False), sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False), sa.Column("before", sa.JSON()), sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_costing_audit_entity", "costing_audit", ["entity"])
    op.create_index("ix_costing_audit_entity_id", "costing_audit", ["entity_id"])
    op.create_table("costing_price_versions", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("title", sa.String(255), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False), sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    for table in ("costing_price_versions", "costing_audit", "costing_revisions", "costing_estimates", "costing_items", "costing_materials", "costing_settings"):
        op.drop_table(table)
    # Keep the PostgreSQL enum member: role history must remain readable.
