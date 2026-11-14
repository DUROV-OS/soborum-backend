"""Connect estimates to CRM, native orders and frozen production recipes."""
from alembic import op
import sqlalchemy as sa
revision = "ce0511202602"
down_revision = "ce0511202601"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("costing_estimates") as batch:
        for name, target in (("crm_deal_id", "crm_entities"), ("sales_order_id", "sales_orders")):
            batch.add_column(sa.Column(name, sa.Integer(), sa.ForeignKey(target+".id", name="fk_costing_estimates_"+name)))
        batch.add_column(sa.Column("sales_order_line", sa.Integer()))
    for name in ("crm_deal_id", "sales_order_id"):
        op.create_index("ix_costing_estimates_"+name, "costing_estimates", [name])
    op.create_table("costing_order_links", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("sales_orders.id"), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("estimate_id", sa.Integer(), sa.ForeignKey("costing_estimates.id"), nullable=False),
        sa.Column("revision_id", sa.Integer(), sa.ForeignKey("costing_revisions.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("local_recipes.id")),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", "line_index", name="uq_costing_order_line"))
    op.create_index("ix_costing_order_links_order_id", "costing_order_links", ["order_id"])
    op.create_index("ix_costing_order_links_estimate_id", "costing_order_links", ["estimate_id"])


def downgrade():
    op.drop_table("costing_order_links")
    for name in ("sales_order_id", "crm_deal_id"):
        op.drop_index("ix_costing_estimates_"+name, table_name="costing_estimates")
    with op.batch_alter_table("costing_estimates") as batch:
        batch.drop_column("sales_order_line")
        for name in ("sales_order_id", "crm_deal_id"):
            batch.drop_constraint("fk_costing_estimates_"+name, type_="foreignkey")
            batch.drop_column(name)
