"""Durable delivery receipts; retain existing lead/review and accounting data."""
from alembic import op
import sqlalchemy as sa

revision = 'cf1411202601'
down_revision = 'pc1011202601'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('marketing_conversion_delivery_control',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('scheduler', sa.String(80), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table('marketing_conversion_delivery_batches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('counter_id', sa.String(20), nullable=False),
        sa.Column('target', sa.String(80), nullable=False),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('payload_sha256', sa.String(64), nullable=False),
        sa.Column('platform_upload_id', sa.String(80)),
        sa.Column('matched_rows', sa.Integer()),
        sa.Column('last_note', sa.String(500), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table('marketing_conversion_delivery_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('marketing_conversion_delivery_batches.id'), nullable=False),
        sa.Column('request_id', sa.Integer(), sa.ForeignKey('client_lead_requests.id'), nullable=False),
        sa.Column('client_id', sa.Integer(), sa.ForeignKey('clients.id'), nullable=False),
        sa.Column('target', sa.String(80), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.UniqueConstraint('client_id', 'target', name='uq_conversion_delivery_client_target'))
    op.create_table('marketing_conversion_delivery_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('marketing_conversion_delivery_batches.id')),
        sa.Column('actor', sa.String(100), nullable=False),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('details', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    for table, columns in (
        ('marketing_conversion_delivery_items', ('batch_id', 'request_id')),
        ('marketing_conversion_delivery_events', ('batch_id',)),
    ):
        for column in columns:
            op.create_index(f'ix_{table}_{column}', table, [column])


def downgrade():
    tables = ('marketing_conversion_delivery_control', 'marketing_conversion_delivery_batches',
              'marketing_conversion_delivery_items', 'marketing_conversion_delivery_events')
    if any(op.get_bind().execute(sa.text(f'SELECT COUNT(*) FROM {table}')).scalar() for table in tables):
        raise RuntimeError('Delivery evidence exists; retain additive tables during application rollback')
    for table in reversed(tables):
        op.drop_table(table)
