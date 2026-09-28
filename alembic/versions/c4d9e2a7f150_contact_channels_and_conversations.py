"""каналы связи карточек и лента переписки (0083-d); сводит головы 0082-a и 0083

Revision ID: c4d9e2a7f150
Revises: b8e4d1f63a27, e7c41b9a2f60
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4d9e2a7f150'
# Две головы: база партнёров (0083-a → 0083-c) и таблицы MAX-бота (0082-a)
# отошли от одного предка d5e2a90c1b77. Переписке нужны обе, и эта миграция
# сводит их в одну — иначе бэкенд с обеими ветками не поднялся бы.
down_revision: Union[str, Sequence[str], None] = ('b8e4d1f63a27', 'e7c41b9a2f60')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUMS = {
    "contact_channel_kind": ("MAX", "TELEGRAM", "WHATSAPP"),
    "contact_channel_status": ("INVITED", "CONNECTED", "STOPPED"),
    "conversation_message_direction": ("IN", "OUT"),
    "conversation_message_delivery": ("SENT", "FAILED"),
}


def upgrade() -> None:
    # Типы создаём явно, в create_table — с create_type=False (приём из d5e2a90c1b77).
    bind = op.get_bind()
    types = {}
    for name, values in ENUMS.items():
        sa.Enum(*values, name=name).create(bind, checkfirst=True)
        types[name] = postgresql.ENUM(*values, name=name, create_type=False)

    op.create_table(
        "contact_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id", ondelete="CASCADE"), nullable=True),
        sa.Column("channel", types["contact_channel_kind"], nullable=False),
        sa.Column("status", types["contact_channel_status"], nullable=False),
        sa.Column("invite_token", sa.String(length=40), nullable=False),
        sa.Column("external_chat_id", sa.String(length=64), nullable=True),
        sa.Column("external_user_name", sa.String(length=255), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("(client_id IS NULL) <> (partner_id IS NULL)", name="ck_contact_channels_one_owner"),
        sa.UniqueConstraint("client_id", "channel", name="uq_contact_channels_client_channel"),
        sa.UniqueConstraint("partner_id", "channel", name="uq_contact_channels_partner_channel"),
        sa.UniqueConstraint("invite_token", name="uq_contact_channels_invite_token"),
    )
    op.create_index("ix_contact_channels_external_chat_id", "contact_channels", ["external_chat_id"])

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "contact_channel_id",
            sa.Integer(),
            sa.ForeignKey("contact_channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", types["conversation_message_direction"], nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("attachments", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("external_message_id", sa.String(length=128), nullable=True),
        sa.Column("delivery", types["conversation_message_delivery"], nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "contact_channel_id", "external_message_id", name="uq_conversation_messages_external"
        ),
    )
    op.create_index(
        "ix_conversation_messages_contact_channel_id", "conversation_messages", ["contact_channel_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_contact_channel_id", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_contact_channels_external_chat_id", table_name="contact_channels")
    op.drop_table("contact_channels")
    bind = op.get_bind()
    for name in reversed(list(ENUMS)):
        sa.Enum(name=name).drop(bind, checkfirst=True)
