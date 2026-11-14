"""Internal projects, immutable discussion/files and links to existing tasks."""
from alembic import op
import json
import sqlalchemy as sa

revision = 'cp1411202601'
down_revision = 'cf1411202601'
branch_labels = None
depends_on = None
TABLES = {'collaboration_projects', 'collaboration_events', 'collaboration_files', 'collaboration_members', 'collaboration_messages', 'collaboration_tasks'}


def upgrade():
    op.create_table('collaboration_projects',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=True),
    sa.Column('deal_id', sa.Integer(), nullable=True),
    sa.Column('production_id', sa.Integer(), nullable=True),
    sa.Column('deadline', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['deal_id'], ['crm_entities.id'], ),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['production_id'], ['productions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('created_by_id', 'request_id')
    )
    op.create_table('collaboration_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['collaboration_projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_collaboration_events_project_id'), 'collaboration_events', ['project_id'], unique=False)
    op.create_table('collaboration_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('uploaded_by_id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('path', sa.String(length=600), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['collaboration_projects.id'], ),
    sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'uploaded_by_id', 'request_id')
    )
    op.create_index(op.f('ix_collaboration_files_project_id'), 'collaboration_files', ['project_id'], unique=False)
    op.create_table('collaboration_members',
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('last_read_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['collaboration_projects.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('project_id', 'user_id')
    )
    op.create_table('collaboration_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('reply_to_id', sa.Integer(), nullable=True),
    sa.Column('file_ids', sa.JSON(), nullable=False),
    sa.Column('mention_ids', sa.JSON(), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['collaboration_projects.id'], ),
    sa.ForeignKeyConstraint(['reply_to_id'], ['collaboration_messages.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'author_id', 'request_id')
    )
    op.create_index(op.f('ix_collaboration_messages_project_id'), 'collaboration_messages', ['project_id'], unique=False)
    op.create_table('collaboration_tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('source_message_id', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['collaboration_projects.id'], ),
    sa.ForeignKeyConstraint(['source_message_id'], ['collaboration_messages.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'created_by_id', 'request_id'),
    sa.UniqueConstraint('project_id', 'task_id')
    )
    op.create_index(op.f('ix_collaboration_tasks_project_id'), 'collaboration_tasks', ['project_id'], unique=False)
    if op.get_bind().dialect.name == 'postgresql':
        for table in ['collaboration_projects', 'collaboration_events', 'collaboration_files', 'collaboration_members', 'collaboration_messages', 'collaboration_tasks']:
            inspector = sa.inspect(op.get_bind())
            keys = inspector.get_pk_constraint(table)['constrained_columns']
            links = [{'column': source, 'table': fk['referred_table'], 'target_column': target} for fk in inspector.get_foreign_keys(table) for source, target in zip(fk['constrained_columns'], fk['referred_columns'])]
            args = ', '.join("'" + json.dumps(value).replace("'", "''") + "'" for value in (keys, links))
            op.execute(f'CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION integrity_capture({args})')
            op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION integrity_reject_mutation()')
        for table in ('collaboration_messages', 'collaboration_files', 'collaboration_events', 'collaboration_tasks'):
            op.execute(f'CREATE TRIGGER collaboration_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION integrity_reject_mutation()')


def downgrade():
    raise RuntimeError('Project records and evidence must be retained; use an application rollback without dropping tables')
