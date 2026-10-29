"""Editable recipes and atomic manufacturing with idempotent requests."""

from alembic import op
import sqlalchemy as sa

revision = "a290c54b8731"
down_revision = "f18b5e603297"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "local_recipes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(255), unique=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "manufacture_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.String(36), unique=True, nullable=False),
        sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("local_recipes.id"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table("manufacture_runs")
    op.drop_table("local_recipes")
