"""Internal team calendar."""
from alembic import op
import sqlalchemy as sa
revision='cal1611202601'
down_revision='cp1411202601'
branch_labels=None
depends_on=None
def upgrade():
    op.create_table('team_calendar_events',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('owner_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),sa.Column('version',sa.Integer(),nullable=False),sa.Column('data',sa.JSON(),nullable=False))
    op.create_index('ix_team_calendar_events_owner_id','team_calendar_events',['owner_id'])
    op.create_table('team_calendar_changes',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('event_id',sa.Integer(),sa.ForeignKey('team_calendar_events.id'),nullable=False),sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),sa.Column('request_id',sa.String(36),unique=True,nullable=False),sa.Column('data',sa.JSON(),nullable=False))
    op.create_index('ix_team_calendar_changes_event_id','team_calendar_changes',['event_id'])
def downgrade():
    op.drop_table('team_calendar_changes');op.drop_table('team_calendar_events')
