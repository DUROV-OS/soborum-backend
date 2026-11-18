"""Authenticated browser SIP calls; additive, private credentials encrypted."""
from alembic import op
import sqlalchemy as sa

revision = 'bp0911202601'
down_revision = 'ch0911202601'
branch_labels = depends_on = None


def upgrade():
    op.create_table('browser_phone_accounts',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('login', sa.String(100), nullable=False, unique=True),
        sa.Column('password_cipher', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('active_call', sa.String(36)),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table('browser_phone_calls',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('call_id', sa.Integer(), sa.ForeignKey('phone_calls.id'), nullable=False, unique=True),
        sa.Column('state', sa.String(30), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True)),
        sa.Column('ended_at', sa.DateTime(timezone=True)),
        sa.Column('recording_started', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    op.drop_table('browser_phone_calls')
    op.drop_table('browser_phone_accounts')
