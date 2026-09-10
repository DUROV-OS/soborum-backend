"""add ai_meeting_transcript_lines table

Revision ID: c4a2f81b6d37
Revises: b7e1d4f2a930
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a2f81b6d37'
down_revision: Union[str, None] = 'b7e1d4f2a930'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_meeting_transcript_lines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('speaker', sa.String(length=32), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('at_ms', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['meeting_id'], ['ai_meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_ai_meeting_transcript_lines_meeting_id'),
        'ai_meeting_transcript_lines',
        ['meeting_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_ai_meeting_transcript_lines_meeting_id'),
        table_name='ai_meeting_transcript_lines',
    )
    op.drop_table('ai_meeting_transcript_lines')
