"""Counterparty requisites, durable comparisons and attributable changes."""
import json
from alembic import op
import sqlalchemy as sa
revision='cparty18112601'
down_revision='drive18112601'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('counterparty_details',sa.Column('counterparty_id',sa.Integer(),sa.ForeignKey('counterparties.id'),primary_key=True),
                    sa.Column('version',sa.Integer(),nullable=False),sa.Column('data',sa.JSON(),nullable=False),
                    sa.Column('updated_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    for table in ('counterparty_bank_accounts','counterparty_contacts'):
        op.create_table(table,sa.Column('id',sa.String(36),primary_key=True),
                        sa.Column('counterparty_id',sa.Integer(),sa.ForeignKey('counterparties.id'),nullable=False),sa.Column('data',sa.JSON(),nullable=False))
        op.create_index('ix_'+table+'_counterparty_id',table,['counterparty_id'])
    op.create_table('counterparty_changes',sa.Column('id',sa.Integer(),primary_key=True),
                    sa.Column('counterparty_id',sa.Integer(),sa.ForeignKey('counterparties.id'),nullable=False),
                    sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
                    sa.Column('request_id',sa.String(36),nullable=False),sa.Column('request_hash',sa.String(64),nullable=False),
                    sa.Column('data',sa.JSON(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
                    sa.UniqueConstraint('actor_id','request_id'))
    op.create_table('counterparty_sync_previews',sa.Column('id',sa.String(36),primary_key=True),
                    sa.Column('counterparty_id',sa.Integer(),sa.ForeignKey('counterparties.id'),nullable=False),
                    sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),sa.Column('data',sa.JSON(),nullable=False),
                    sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    for table in ('counterparty_changes','counterparty_sync_previews'): op.create_index('ix_'+table+'_counterparty_id',table,['counterparty_id'])
    conn=op.get_bind()
    # Link by existing supplier ID only. Names and tax IDs are never guessed.
    conn.execute(sa.text("INSERT INTO counterparties (name,kind,supplier_id,is_active) SELECT s.name,'SUPPLIER',s.id,lower(cast(s.status as varchar))!='archived' FROM suppliers s WHERE NOT EXISTS (SELECT 1 FROM counterparties c WHERE c.supplier_id=s.id)"))
    if conn.dialect.name=='postgresql':
        inspector=sa.inspect(conn)
        for table in ('counterparty_details','counterparty_bank_accounts','counterparty_contacts','counterparty_changes','counterparty_sync_previews'):
            pk=['counterparty_id'] if table=='counterparty_details' else ['id']
            relations=[{'column':src,'table':fk['referred_table'],'target_column':dst} for fk in inspector.get_foreign_keys(table) for src,dst in zip(fk['constrained_columns'],fk['referred_columns'])]
            args=', '.join("'"+json.dumps(v).replace("'","''")+"'" for v in (pk,relations))
            op.execute(f'CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION integrity_capture({args})')
            op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION integrity_reject_mutation()')
        for table in ('counterparty_changes','counterparty_sync_previews'):
            op.execute(f'CREATE TRIGGER counterparty_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION integrity_reject_mutation()')


def downgrade():
    raise RuntimeError('Preserve requisites and evidence; use a compatible application rollback.')
