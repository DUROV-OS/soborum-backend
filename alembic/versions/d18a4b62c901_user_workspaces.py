"""Persist staff job role and personal workspace settings."""
from alembic import op
import sqlalchemy as sa

revision = "d18a4b62c901"
down_revision = "b3e91d6042a8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("job_role", sa.String(20), nullable=True))
    op.add_column("users", sa.Column("workspace_preferences", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("users", "workspace_preferences")
    op.drop_column("users", "job_role")
