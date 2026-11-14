"""Keep reviewed CRM projections separate from immutable imported originals."""
from alembic import op
import sqlalchemy as sa

revision = 'cf0511202601'
down_revision = 'ce0511202603'
branch_labels = None
depends_on = None


def upgrade():
    # No backfill, inferred classification, permission change or stage movement.
    op.create_table('crm_import_reviews',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source_record_id', sa.Integer(), sa.ForeignKey('moysklad_records.id'), nullable=False),
        sa.Column('attachment_index', sa.Integer(), nullable=False),
        sa.Column('disposition', sa.String(20), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('content_digest', sa.String(64), nullable=True),
        sa.Column('public_data', sa.JSON(), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index('ix_crm_import_review_source', 'crm_import_reviews', ['source_record_id', 'attachment_index', 'id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM crm_import_reviews')).scalar():
        raise RuntimeError('Classification history is not empty; retain the table when rolling back the application')
    op.drop_table('crm_import_reviews')
