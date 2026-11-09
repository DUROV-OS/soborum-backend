"""Versioned production reference drafts and scoped project links."""
from alembic import op
import sqlalchemy as sa

revision='ce0511202603'
down_revision='ce0511202602'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('house_technical_cards',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('model_key',sa.String(64),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('content',sa.JSON(),nullable=False),
        sa.Column('source_hash',sa.String(64),nullable=False),
        sa.Column('manually_edited',sa.Boolean(),nullable=False),
        sa.Column('updated_by_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_index('ix_house_technical_cards_model_key','house_technical_cards',['model_key'],unique=True)


def downgrade():
    op.drop_table('house_technical_cards')
