"""Merge current sales schema and private calls; scope calls to one application."""
from alembic import op
import sqlalchemy as sa

revision = 'e48d7b1c306a'
down_revision = ('b572f3d01a62', 'd287f63b910a')
branch_labels = depends_on = None


def upgrade():
    op.add_column('phone_calls', sa.Column('deal_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_phone_calls_deal_id', 'phone_calls', 'crm_entities', ['deal_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_phone_calls_deal_id', 'phone_calls', ['deal_id'])
    op.add_column('phone_calls', sa.Column('deal_match_status', sa.String(30), nullable=False, server_default='unmatched'))
    # Earlier manual links used crm_entity_id for the application itself.
    op.execute("UPDATE phone_calls SET deal_id = crm_entity_id, deal_match_status = 'manual' WHERE crm_entity_id IN (SELECT id FROM crm_entities WHERE kind = 'deal')")
    op.create_table('crm_chat_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entity_id', sa.Integer(), sa.ForeignKey('crm_entities.id', ondelete='CASCADE'), nullable=False),
        sa.Column('max_chat_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('label', sa.String(200), nullable=False))
    op.create_index('ix_crm_chat_links_entity_id', 'crm_chat_links', ['entity_id'])


def downgrade():
    op.drop_table('crm_chat_links')
    op.drop_column('phone_calls', 'deal_match_status')
    op.drop_index('ix_phone_calls_deal_id', 'phone_calls')
    op.drop_constraint('fk_phone_calls_deal_id', 'phone_calls', type_='foreignkey')
    op.drop_column('phone_calls', 'deal_id')
