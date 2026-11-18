"""Personal and company drive with immutable file revisions."""
from alembic import op
import sqlalchemy as sa

revision = 'drive18112601'
down_revision = 'meet1811202601'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('drive_folders',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('owner_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('scope',sa.String(16),nullable=False),sa.Column('name',sa.String(200),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_table('drive_files',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('owner_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('folder_id',sa.String(36),sa.ForeignKey('drive_folders.id')),
        sa.Column('scope',sa.String(16),nullable=False),sa.Column('name',sa.String(255),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),sa.Column('deleted',sa.Boolean(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_table('drive_revisions',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('file_id',sa.String(36),sa.ForeignKey('drive_files.id'),nullable=False),
        sa.Column('number',sa.Integer(),nullable=False),sa.Column('filename',sa.String(255),nullable=False),
        sa.Column('size',sa.Integer(),nullable=False),sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('media_type',sa.String(120),nullable=False),
        sa.Column('author_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('file_id','number'))
    op.create_table('drive_changes',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('file_id',sa.String(36),sa.ForeignKey('drive_files.id')),
        sa.Column('folder_id',sa.String(36),sa.ForeignKey('drive_folders.id')),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('request_id',sa.String(36),nullable=False),sa.Column('request_hash',sa.String(64),nullable=False),
        sa.Column('action',sa.String(24),nullable=False),sa.Column('data',sa.JSON(),nullable=False),
        sa.Column('previous_hash',sa.String(64),nullable=False),sa.Column('hash',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('actor_id','request_id'))
    for table,fields in {'drive_folders':['owner_id'],'drive_files':['owner_id','folder_id','scope','deleted'],
                         'drive_revisions':['file_id'],'drive_changes':['file_id']}.items():
        for name in fields:op.create_index('ix_'+table+'_'+name,table,[name])


def downgrade():
    for table in ('drive_changes','drive_revisions','drive_files','drive_folders'):op.drop_table(table)
