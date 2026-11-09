"""Named approvals, evidenced monthly overhead and immutable review records."""
import json
from alembic import op
import sqlalchemy as sa

revision='ca0911202601'
down_revision='oc0911202601'
branch_labels=depends_on=None
TABLES=('costing_approval_policy','costing_signoffs','costing_overhead_plans','costing_evidence_documents')


def upgrade():
    connection=op.get_bind()
    if connection.dialect.name=='postgresql':
        op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'COSTING_EVIDENCE'")
    op.create_table('costing_approval_policy',sa.Column('id',sa.Integer(),primary_key=True),
        *[sa.Column(stage+'_user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=True) for stage in ('production','accounting','director')])
    op.create_table('costing_signoffs',sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('target',sa.String(16),nullable=False),sa.Column('target_id',sa.Integer(),nullable=False),
        sa.Column('stage',sa.String(16),nullable=False),sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('revision_id',sa.Integer(),sa.ForeignKey('costing_revisions.id')),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('request_id',sa.String(36),nullable=False,unique=True),sa.Column('fingerprint',sa.String(64),nullable=False),
        sa.Column('content',sa.JSON(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_table('costing_overhead_plans',sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('month',sa.Date(),nullable=False),sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),sa.Column('content',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('month','version',name='uq_costing_month_version'))
    op.create_table('costing_evidence_documents',sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('target',sa.String(16),nullable=False),sa.Column('target_id',sa.Integer(),nullable=False),
        sa.Column('file_id',sa.Integer(),sa.ForeignKey('file_assets.id'),nullable=False,unique=True),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),sa.Column('title',sa.String(255),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    for table,fields in (('costing_signoffs',('target','target_id')),('costing_overhead_plans',('month',)),('costing_evidence_documents',('target','target_id'))):
        for field in fields: op.create_index('ix_'+table+'_'+field,table,[field])
    if connection.dialect.name=='postgresql':
        inspector=sa.inspect(connection)
        for table in TABLES:
            relations=[{'column':src,'table':fk['referred_table'],'target_column':dst} for fk in inspector.get_foreign_keys(table)
                       for src,dst in zip(fk['constrained_columns'],fk['referred_columns'])]
            args=', '.join("'"+json.dumps(value).replace("'","''")+"'" for value in (['id'],relations))
            op.execute(f'CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_capture({args})')
            op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON public."{table}" FOR EACH STATEMENT EXECUTE FUNCTION public.integrity_reject_mutation()')
        for table in ('costing_signoffs','costing_overhead_plans','costing_evidence_documents','costing_revisions'):
            op.execute(f'CREATE TRIGGER costing_version_immutable BEFORE UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_reject_mutation()')
        connection.execute(sa.text("SELECT set_config('durov.actor', :actor, true)"),{'actor':json.dumps({'kind':'schema_migration','revision':revision})})
    # Scope only the three identities explicitly designated by the owner. No grants or roles are changed.
    ids={stage:connection.execute(sa.text('SELECT id FROM users WHERE lower(email)=:email AND is_active=true'),{'email':email}).scalar()
         for stage,email in {'production':'vrn.sfera@yandex.ru','accounting':'tanya_200872@mail.ru','director':'sales@durov.house'}.items()}
    connection.execute(sa.text('INSERT INTO costing_approval_policy (id,production_user_id,accounting_user_id,director_user_id) VALUES (1,:production,:accounting,:director)'),ids)


def downgrade():
    raise RuntimeError('Retain approved calculations and evidence. Use a compatible application fallback.')
