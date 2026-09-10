"""add ai_meeting_notes table

Revision ID: d9b3e6f1a248
Revises: c4a2f81b6d37
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9b3e6f1a248'
down_revision: Union[str, None] = 'c4a2f81b6d37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_meeting_notes',
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('decisions', sa.JSON(), nullable=False),
        sa.Column('tasks', sa.JSON(), nullable=False),
        sa.Column('questions', sa.JSON(), nullable=False),
        sa.Column('source_line_count', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['meeting_id'], ['ai_meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('meeting_id'),
    )


def downgrade() -> None:
    op.drop_table('ai_meeting_notes')
