"""agent_approvals.subject_snapshot/subject_hash: что именно согласовано (0084-e)

Revision ID: b3f8d1e6c527
Revises: a7c4e9f2b813
Create Date: 2026-09-29 00:00:01.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f8d1e6c527"
down_revision: Union[str, None] = "a7c4e9f2b813"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Снимок согласуемого пункта (agent, stance, citations, legal_verdict) и
    # sha256 от него. Старые согласования — NULL: снимка нет, решение по ним
    # сервис не примет (409), их заменит следующая смена.
    op.add_column("agent_approvals", sa.Column("subject_snapshot", sa.JSON(), nullable=True))
    op.add_column("agent_approvals", sa.Column("subject_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_approvals", "subject_hash")
    op.drop_column("agent_approvals", "subject_snapshot")
