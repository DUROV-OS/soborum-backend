"""Private calendar attachments."""
from alembic import op
import sqlalchemy as sa
revision='cal1711202601'
down_revision='cal1611202601'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('team_calendar_files',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('uploaded_by_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('event_id',sa.Integer(),sa.ForeignKey('team_calendar_events.id')),
        sa.Column('request_id',sa.String(36),nullable=False),
        sa.Column('filename',sa.String(255),nullable=False),
        sa.Column('size',sa.Integer(),nullable=False),
        sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('media_type',sa.String(120),nullable=False),
        sa.UniqueConstraint('uploaded_by_id','request_id'))
    op.create_index('ix_team_calendar_files_uploaded_by_id','team_calendar_files',['uploaded_by_id'])
    op.create_index('ix_team_calendar_files_event_id','team_calendar_files',['event_id'])

def downgrade():
    op.drop_table('team_calendar_files')
