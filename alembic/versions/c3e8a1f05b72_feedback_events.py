"""feedback request events and author_seen_event_id (0090)

Revision ID: c3e8a1f05b72
Revises: b8d4f20a6c31
Create Date: 2026-10-04 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3e8a1f05b72'
down_revision: Union[str, None] = 'b8d4f20a6c31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL = автор ещё не открывал заявку. У старых заявок лента пуста
    # (прошлые смены статуса не записывались), непрочитанного там не будет.
    op.add_column("feedback_requests", sa.Column("author_seen_event_id", sa.Integer(), nullable=True))

    # feedback_status уже создан в a7c1e4b95d30 — переиспользуем, не создаём.
    feedback_status = postgresql.ENUM(
        "NEW", "IN_PROGRESS", "DONE", "REJECTED", name="feedback_status", create_type=False
    )
    op.create_table(
        "feedback_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feedback_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("STATUS", "COMMENT", "CHANGE", name="feedback_event_kind"), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("old_status", feedback_status, nullable=True),
        sa.Column("new_status", feedback_status, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["feedback_id"], ["feedback_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_feedback_events_feedback_id"), "feedback_events", ["feedback_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_feedback_events_feedback_id"), table_name="feedback_events")
    op.drop_table("feedback_events")
    sa.Enum(name="feedback_event_kind").drop(op.get_bind(), checkfirst=True)
    op.drop_column("feedback_requests", "author_seen_event_id")
