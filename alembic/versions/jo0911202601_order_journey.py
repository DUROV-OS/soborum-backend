"""Keep sales and procurement handoffs append-only at the database boundary."""
from alembic import op

revision = 'jo0911202601'
down_revision = 'ac0911202601'
branch_labels = None
depends_on = None
TABLES = ('sales_events',)


def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('CREATE TRIGGER journey_events_immutable BEFORE UPDATE OR DELETE ON public.sales_events FOR EACH ROW EXECUTE FUNCTION public.integrity_reject_mutation()')


def downgrade():
    raise RuntimeError('Retain the sales and procurement audit trail; use a compatible application fallback.')
