"""Scope attachments to supplier invoices and receipts."""

from alembic import op
import sqlalchemy as sa

revision = 'pdocf20112601'
down_revision = 'ivsts19112601'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'purchase_document_files',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('receipt_id', sa.Integer(), sa.ForeignKey('purchase_receipts.id')),
        sa.Column('invoice_id', sa.Integer(), sa.ForeignKey('moysklad_records.id')),
        sa.Column('asset_id', sa.Integer(), sa.ForeignKey('file_assets.id'), nullable=False, unique=True),
        sa.Column('request_id', sa.String(36), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('(receipt_id IS NULL) <> (invoice_id IS NULL)', name='ck_purchase_document_file_scope'),
    )
    op.create_index('ix_purchase_document_files_receipt_id', 'purchase_document_files', ['receipt_id'])
    op.create_index('ix_purchase_document_files_invoice_id', 'purchase_document_files', ['invoice_id'])


def downgrade():
    op.drop_table('purchase_document_files')
