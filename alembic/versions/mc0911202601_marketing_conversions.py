"""Retain safe attribution, deduplicate provider retries, and audit sales feedback."""
from alembic import op
import sqlalchemy as sa

revision = 'mc0911202601'
down_revision = 'cg0611202601'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('client_lead_requests', sa.Column('external_event_id', sa.String(255), nullable=True))
    op.add_column('client_lead_requests', sa.Column('tracking', sa.JSON(), nullable=True))
    op.create_unique_constraint('uq_lead_external_event', 'client_lead_requests', ['external_event_id'])
    op.create_table('marketing_conversion_reviews',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('request_id', sa.Integer(), sa.ForeignKey('client_lead_requests.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('outcome', sa.String(30), nullable=False),
        sa.Column('reason', sa.String(1000), nullable=False),
        sa.Column('facts', sa.JSON(), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('request_id', 'version', name='uq_conversion_review_version'))
    op.create_index('ix_marketing_conversion_reviews_request_id', 'marketing_conversion_reviews', ['request_id'])


def downgrade():
    db = op.get_bind()
    if db.execute(sa.text('SELECT COUNT(*) FROM marketing_conversion_reviews')).scalar() or db.execute(sa.text('SELECT COUNT(*) FROM client_lead_requests WHERE external_event_id IS NOT NULL OR tracking IS NOT NULL')).scalar():
        raise RuntimeError('Conversion evidence exists; keep additive schema during application rollback')
    op.drop_index('ix_marketing_conversion_reviews_request_id', table_name='marketing_conversion_reviews')
    op.drop_table('marketing_conversion_reviews')
    op.drop_constraint('uq_lead_external_event', 'client_lead_requests', type_='unique')
    op.drop_column('client_lead_requests', 'tracking')
    op.drop_column('client_lead_requests', 'external_event_id')
