"""feedback requests and attachments (0075)

Revision ID: a7c1e4b95d30
Revises: 19c9bcb0f3e3
Create Date: 2026-09-21 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a7c1e4b95d30'
down_revision: Union[str, None] = '19c9bcb0f3e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Новое значение существующего pg-типа file_purpose — autogenerate такие
    # добавления не видит, тот же приём, что в 9fce253f8073.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'FEEDBACK_ATTACHMENT'")

    op.create_table(
        "feedback_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("NEW", "IN_PROGRESS", "DONE", "REJECTED", name="feedback_status"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_feedback_requests_author_id"), "feedback_requests", ["author_id"])

    op.create_table(
        "feedback_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feedback_id", sa.Integer(), nullable=False),
        sa.Column("file_asset_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("SCREENSHOT", "LOG", name="feedback_attachment_kind"), nullable=False),
        sa.ForeignKeyConstraint(["feedback_id"], ["feedback_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_asset_id"], ["file_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_feedback_attachments_feedback_id"), "feedback_attachments", ["feedback_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_feedback_attachments_feedback_id"), table_name="feedback_attachments")
    op.drop_table("feedback_attachments")
    op.drop_index(op.f("ix_feedback_requests_author_id"), table_name="feedback_requests")
    op.drop_table("feedback_requests")
    sa.Enum(name="feedback_attachment_kind").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="feedback_status").drop(op.get_bind(), checkfirst=True)
    # Значение FEEDBACK_ATTACHMENT в enum file_purpose остаётся: в Postgres
    # нет DROP VALUE (тот же компромисс, что в 9fce253f8073).
