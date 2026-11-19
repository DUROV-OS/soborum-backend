"""Editable supplier invoice status dictionary and audited choices."""

import json

from alembic import op
import sqlalchemy as sa

revision = 'ivsts19112601'
down_revision = 'poorg19112601'
branch_labels = None
depends_on = None


def upgrade():
    statuses = op.create_table(
        'supplier_invoice_statuses',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(16), unique=True),
        sa.Column('label', sa.String(64), nullable=False),
        sa.Column('label_key', sa.String(128), nullable=False, unique=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
    )
    op.bulk_insert(statuses, [
        {'code': 'paid', 'label': 'Оплачено', 'label_key': 'оплачено'},
        {'code': 'unpaid', 'label': 'Не оплачено', 'label_key': 'не оплачено'},
        {'code': 'waiting', 'label': 'Ожидаем', 'label_key': 'ожидаем'},
    ])
    op.create_table(
        'supplier_invoice_status_choices',
        sa.Column('invoice_id', sa.Integer(), sa.ForeignKey('moysklad_records.id'), primary_key=True),
        sa.Column('status_id', sa.Integer(), sa.ForeignKey('supplier_invoice_statuses.id')),
        sa.Column('updated_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
    )
    conn = op.get_bind()
    if conn.dialect.name == 'postgresql':
        inspector = sa.inspect(conn)
        for table, field in [('supplier_invoice_statuses', 'id'), ('supplier_invoice_status_choices', 'invoice_id')]:
            refs = [{'column': src, 'table': fk['referred_table'], 'target_column': dest}
                    for fk in inspector.get_foreign_keys(table)
                    for src, dest in zip(fk['constrained_columns'], fk['referred_columns'])]
            op.execute(f"CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON {table} "
                       f"FOR EACH ROW EXECUTE FUNCTION integrity_capture('{json.dumps([field])}', '{json.dumps(refs)}')")
            op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON {table} '
                       'FOR EACH STATEMENT EXECUTE FUNCTION integrity_reject_mutation()')


def downgrade():
    op.drop_table('supplier_invoice_status_choices')
    op.drop_table('supplier_invoice_statuses')
