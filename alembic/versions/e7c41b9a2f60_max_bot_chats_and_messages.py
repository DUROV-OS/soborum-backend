"""чаты и сообщения MAX-бота (0082-a)

Revision ID: e7c41b9a2f60
Revises: b8d4f20a6c31
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7c41b9a2f60'
down_revision: Union[str, None] = 'b8d4f20a6c31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "max_bot_chats",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("dialog_user_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_event_time", sa.BigInteger(), nullable=True),
        sa.Column("unread", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_table(
        "max_bot_messages",
        sa.Column("mid", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("sender_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_name", sa.String(length=255), nullable=True),
        sa.Column("is_outgoing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("mid"),
    )
    op.create_index("ix_max_bot_messages_chat_id", "max_bot_messages", ["chat_id"])
    op.create_index("ix_max_bot_messages_timestamp", "max_bot_messages", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_max_bot_messages_timestamp", table_name="max_bot_messages")
    op.drop_index("ix_max_bot_messages_chat_id", table_name="max_bot_messages")
    op.drop_table("max_bot_messages")
    op.drop_table("max_bot_chats")
