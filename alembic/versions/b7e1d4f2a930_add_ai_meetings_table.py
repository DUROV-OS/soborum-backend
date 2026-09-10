"""add ai_meetings table and meeting_audio file purpose

Revision ID: b7e1d4f2a930
Revises: e2f9a4c7b108
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e1d4f2a930'
down_revision: Union[str, None] = 'e2f9a4c7b108'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New FilePurpose.MEETING_AUDIO value for the recorded «Совещание» blob.
    # Autogenerate doesn't detect added enum values on an existing pg enum
    # type, so this is added by hand (see 5b3615d19b4e for the same pattern).
    op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'MEETING_AUDIO'")

    op.create_table(
        'ai_meetings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column(
            'status',
            sa.Enum('RECORDING', 'FINISHED', name='ai_meeting_status'),
            nullable=False,
        ),
        sa.Column('audio_file_id', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['audio_file_id'], ['file_assets.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('ai_meetings')
    op.execute("DROP TYPE IF EXISTS ai_meeting_status")
    # Postgres has no DROP VALUE for enums; 'MEETING_AUDIO' is left in place.
