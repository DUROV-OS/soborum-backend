"""Shared CRM tasks and deterministic lead attention settings."""

from alembic import op
import sqlalchemy as sa

revision = "d74b209ea631"
down_revision = "c62e174b9083"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crm_task_links",
        sa.Column("entity_id", sa.Integer(), sa.ForeignKey("crm_entities.id"), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False, unique=True),
    )
    op.create_table(
        "crm_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("response_hours", sa.Integer(), nullable=False),
        sa.Column("idle_days", sa.Integer(), nullable=False),
    )
    op.execute("INSERT INTO crm_settings (id, response_hours, idle_days) VALUES (1, 24, 3)")


def downgrade():
    op.drop_table("crm_task_links")
    op.drop_table("crm_settings")
