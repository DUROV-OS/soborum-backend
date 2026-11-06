"""Keep normalized agency logos in CRM, outside shared documents and file paths."""
from alembic import op
import sqlalchemy as sa

revision = 'cg0611202601'
down_revision = 'cf0511202601'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('crm_agency_logos',
        sa.Column('agency_id', sa.Integer(), sa.ForeignKey('crm_entities.id'), primary_key=True),
        sa.Column('image_data', sa.LargeBinary(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('uploaded_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('width > 0 AND width <= 1024 AND height > 0 AND height <= 1024', name='ck_crm_logo_dimensions'),
        sa.CheckConstraint('byte_size > 0 AND byte_size <= 5242880 AND length(image_data) = byte_size', name='ck_crm_logo_size'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM crm_agency_logos')).scalar():
        raise RuntimeError('Agency logos are not empty; retain the table during application rollback')
    op.drop_table('crm_agency_logos')
