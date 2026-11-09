"""Add optional employee work phone and PBX extension, without assigning numbers."""

from alembic import op
import sqlalchemy as sa

revision = "e398a47c621b"
down_revision = "d287f63b910a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("work_phone", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("telephony_extension", sa.String(10), nullable=True))


def downgrade():
    op.drop_column("users", "telephony_extension")
    op.drop_column("users", "work_phone")
