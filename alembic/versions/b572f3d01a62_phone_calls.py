"""Private phone archive and human review trail; no existing data is changed."""
from alembic import op
import sqlalchemy as sa

revision = 'b572f3d01a62'
down_revision = 'a672d319e085'
branch_labels = depends_on = None


def upgrade():
    op.create_table('phone_calls',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('provider', sa.String(30), nullable=False),
        sa.Column('external_id', sa.String(255), nullable=False),
        sa.Column('phone', sa.String(40), nullable=False),
        sa.Column('direction', sa.String(10), nullable=False),
        sa.Column('employee', sa.String(255), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('duration', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('client_id', sa.Integer(), sa.ForeignKey('clients.id', ondelete='SET NULL')),
        sa.Column('crm_entity_id', sa.Integer(), sa.ForeignKey('crm_entities.id', ondelete='SET NULL')),
        sa.Column('match_status', sa.String(30), nullable=False),
        sa.Column('recording_url', sa.Text(), nullable=False),
        sa.Column('audio_ready', sa.Boolean(), nullable=False),
        sa.Column('processing', sa.String(30), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('error', sa.String(1000), nullable=False),
        sa.Column('transcript', sa.Text(), nullable=False),
        sa.Column('segments', sa.JSON(), nullable=False),
        sa.Column('analysis', sa.JSON(), nullable=False),
        sa.Column('reviewed_at', sa.DateTime(timezone=True)),
        sa.Column('reviewed_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('provider','external_id',name='uq_phone_call_provider'))
    for field in ('phone','client_id','crm_entity_id','processing'):
        op.create_index('ix_phone_calls_'+field,'phone_calls',[field])
    op.create_table('phone_call_audit',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('call_id',sa.Integer(),sa.ForeignKey('phone_calls.id'),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('action',sa.String(40),nullable=False),
        sa.Column('details',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_index('ix_phone_call_audit_call_id','phone_call_audit',['call_id'])


def downgrade():
    op.drop_table('phone_call_audit')
    op.drop_table('phone_calls')
