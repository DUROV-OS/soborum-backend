"""add is_assistant_query to ai_meeting_transcript_lines

Revision ID: e1c7a4d92b60
Revises: d9b3e6f1a248
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1c7a4d92b60'
down_revision: Union[str, None] = 'd9b3e6f1a248'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ai_meeting_transcript_lines',
        sa.Column(
            'is_assistant_query',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column('ai_meeting_transcript_lines', 'is_assistant_query', server_default=None)


def downgrade() -> None:
    op.drop_column('ai_meeting_transcript_lines', 'is_assistant_query')
