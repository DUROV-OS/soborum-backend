"""add meeting circumstances: location, participants, occurred_at

Revision ID: f2d4a6c8e015
Revises: e1c7a4d92b60
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2d4a6c8e015'
down_revision: Union[str, None] = 'e1c7a4d92b60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_meetings', sa.Column('location', sa.String(length=255), nullable=True))
    op.add_column('ai_meetings', sa.Column('participants', sa.String(length=500), nullable=True))
    op.add_column('ai_meetings', sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_meetings', 'occurred_at')
    op.drop_column('ai_meetings', 'participants')
    op.drop_column('ai_meetings', 'location')
