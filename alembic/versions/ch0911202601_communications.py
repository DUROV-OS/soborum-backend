"""Private phone archive and human review trail; no existing data is changed."""
from alembic import op
import sqlalchemy as sa

revision = 'ch0911202601'
down_revision = 'mc0911202601'
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
        sa.Column('deal_id', sa.Integer(), sa.ForeignKey('crm_entities.id', ondelete='SET NULL')),
        sa.Column('deal_match_status', sa.String(30), nullable=False),
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
    for field in ('phone','client_id','crm_entity_id','deal_id','processing'):
        op.create_index('ix_phone_calls_'+field,'phone_calls',[field])
    op.create_table('phone_call_audit',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('call_id',sa.Integer(),sa.ForeignKey('phone_calls.id'),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('action',sa.String(40),nullable=False),
        sa.Column('details',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_index('ix_phone_call_audit_call_id','phone_call_audit',['call_id'])

    op.create_table('phone_routing_config',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('main_phone', sa.String(16), nullable=False),
        sa.Column('cursor', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('wait_seconds', sa.Integer(), nullable=False),
        sa.Column('pbx_setup_confirmed', sa.Boolean(), nullable=False),
        sa.Column('auto_create_leads', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('max_incoming_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('max_leads_started_at', sa.DateTime(timezone=True)),
        sa.Column('lead_stage_id', sa.Integer(), sa.ForeignKey('crm_entities.id')),
        sa.Column('lead_actor_id', sa.Integer(), sa.ForeignKey('users.id')))
    op.execute(sa.text("INSERT INTO phone_routing_config (id,enabled,main_phone,cursor,version,wait_seconds,pbx_setup_confirmed,auto_create_leads) VALUES (1,false,'79066773555',0,1,20,false,false)"))
    op.create_table('phone_routing_members',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('pbx_user', sa.String(100), nullable=False, unique=True),
        sa.Column('enabled', sa.Boolean(), nullable=False))
    op.create_table('phone_routing_memory',
        sa.Column('phone', sa.String(16), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('source', sa.String(20), nullable=False),
        sa.Column('confirmed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index('ix_phone_routing_memory_user_id', 'phone_routing_memory', ['user_id'])
    op.create_table('phone_routing_audit',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('details', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table('phone_dial_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('phone', sa.String(16), nullable=False),
        sa.Column('pbx_user', sa.String(100), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('actor_id', 'request_id', name='uq_phone_dial_request'))
    op.create_table('max_inbound_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.String(40), nullable=False),
        sa.Column('chat_id', sa.String(40), nullable=False),
        sa.Column('message_id', sa.String(100), nullable=False),
        sa.Column('entity_id', sa.Integer(), sa.ForeignKey('crm_entities.id')),
        sa.Column('state', sa.String(30), nullable=False),
        sa.Column('phone', sa.String(16), nullable=False, server_default=''),
        sa.Column('sender_name', sa.String(255), nullable=False, server_default=''),
        sa.Column('message_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('account_id', 'chat_id', 'message_id', name='uq_max_inbound_message'))
    op.create_index('ix_max_inbound_messages_entity_id', 'max_inbound_messages', ['entity_id'])


def downgrade():
    for name in ('max_inbound_messages', 'phone_dial_requests', 'phone_routing_audit', 'phone_routing_memory', 'phone_routing_members', 'phone_routing_config', 'phone_call_audit', 'phone_calls'):
        op.drop_table(name)
