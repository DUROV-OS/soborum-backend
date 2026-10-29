"""Local contacts, configurable sales pipelines, deals, tasks and audit history."""

from alembic import op
import sqlalchemy as sa

revision = "f18b5e603297"
down_revision = "e29a7310c284"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crm_entities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("source_id", sa.String(255)),
        sa.Column("name", sa.String(1000), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("kind", "source_id", name="uq_crm_source"),
    )
    op.create_index("ix_crm_entities_kind", "crm_entities", ["kind"])
    op.create_index("ix_crm_entities_archived", "crm_entities", ["archived"])
    op.create_table(
        "crm_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_id", sa.Integer(), sa.ForeignKey("crm_entities.id"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_crm_events_entity_id", "crm_events", ["entity_id"])


def downgrade():
    op.drop_table("crm_events")
    op.drop_table("crm_entities")
