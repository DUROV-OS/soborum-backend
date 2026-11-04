"""Add CRM MAX links without modifying existing chat bindings."""
from alembic import op
import sqlalchemy as sa

revision = 'f4a9b58d732c'
down_revision = 'e398a47c621b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('crm_chat_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entity_id', sa.Integer(), sa.ForeignKey('crm_entities.id', ondelete='CASCADE'), nullable=False),
        sa.Column('max_chat_id', sa.BigInteger(), nullable=False),
        sa.Column('label', sa.String(255), nullable=False),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('max_chat_id', name='uq_crm_chat_links_max_chat_id'))
    op.create_index('ix_crm_chat_links_entity_id', 'crm_chat_links', ['entity_id'])


def downgrade():
    op.drop_index('ix_crm_chat_links_entity_id', table_name='crm_chat_links')
    op.drop_table('crm_chat_links')
