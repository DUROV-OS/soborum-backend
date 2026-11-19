"""Link supplier orders to the ordering organization.

Revision ID: poorg19112601
Revises: cparty18112601
"""

import json

from alembic import op
import sqlalchemy as sa


revision = 'poorg19112601'
down_revision = 'cparty18112601'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('supplier_orders', sa.Column('organization_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_supplier_orders_organization', 'supplier_orders', 'organizations', ['organization_id'], ['id'])

    conn = op.get_bind()
    organizations = sa.table('organizations', sa.column('id', sa.Integer()))
    links = sa.table('moysklad_links', sa.column('source_key'), sa.column('target_type'), sa.column('target_id', sa.Integer()))
    refs = dict(conn.execute(sa.select(links.c.source_key, links.c.target_id)
        .join(organizations, organizations.c.id == links.c.target_id)
        .where(links.c.target_type == 'organizations')).tuples().all())
    if not refs:
        return

    orders = sa.table('supplier_orders', sa.column('id', sa.Integer()), sa.column('source_id'),
                      sa.column('organization_id', sa.Integer()))
    sources = sa.table('moysklad_records', sa.column('entity_type'), sa.column('source_id'),
                       sa.column('payload', sa.JSON()))
    matches = conn.execute(sa.select(orders.c.id, sources.c.payload)
        .join(sources, sources.c.source_id == orders.c.source_id)
        .where(sources.c.entity_type == 'purchaseorder', orders.c.organization_id.is_(None))).all()
    updates = []
    for order_id, payload in matches:
        organization = payload.get('organization') or {}
        reference = (organization.get('meta') or {}).get('href', '').split('?', 1)[0].rstrip('/')
        source_id = organization.get('id') or reference.rsplit('/', 1)[-1]
        target = refs.get('organization/' + source_id)
        if target is not None:
            updates.append({'order_id': order_id, 'org_id': target})
    if updates:
        if conn.dialect.name == 'postgresql':
            conn.execute(sa.text("SELECT set_config('durov.actor', :actor, true)"), {
                'actor': json.dumps({'kind': 'schema_migration', 'revision': revision, 'source': 'moysklad_purchaseorder'}),
            })
        conn.execute(orders.update().where(orders.c.id == sa.bindparam('order_id'))
                     .values(organization_id=sa.bindparam('org_id')), updates)


def downgrade():
    op.drop_constraint('fk_supplier_orders_organization', 'supplier_orders', type_='foreignkey')
    op.drop_column('supplier_orders', 'organization_id')
