"""Durable meeting transcription and reviewed action plans."""
from alembic import op
import sqlalchemy as sa

revision = 'meet1811202601'
down_revision = 'cal1711202601'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ai_meeting_processing',
        sa.Column('meeting_id', sa.Integer(), sa.ForeignKey('ai_meetings.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('lease_token', sa.String(36)),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    op.drop_table('ai_meeting_processing')
