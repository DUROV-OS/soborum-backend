"""add telegram bridge tables

Revision ID: f5a1c7e93b28
Revises: e4b2d7f36a19
Create Date: 2026-09-06 03:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f5a1c7e93b28'
down_revision: Union[str, None] = 'e4b2d7f36a19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Новое значение FilePurpose.TELEGRAM_INGEST для медиа, которое ежедневная
    # задача вытаскивает из наблюдаемого Telegram-чата. Автогенерация не видит
    # добавленных значений pg-enum — вручную, как в 5b3615d19b4e.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'TELEGRAM_INGEST'")

    op.create_table(
        "telegram_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_user_id", sa.String(length=64), nullable=True),
        sa.Column("tg_username", sa.String(length=255), nullable=True),
        sa.Column("sender_name", sa.String(length=255), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("file_unique_id", sa.String(length=128), nullable=True),
        sa.Column("file_asset_id", sa.Integer(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_into_kb_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["file_asset_id"], ["file_assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_telegram_chat_message"),
    )
    op.create_index("ix_telegram_messages_window", "telegram_messages", ["chat_id", "sent_at"])
    op.create_index("ix_telegram_messages_tg_user_id", "telegram_messages", ["tg_user_id"])

    op.create_table(
        "telegram_account_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.String(length=64), nullable=False),
        sa.Column("tg_username", sa.String(length=255), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("alias", sa.String(length=64), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tg_user_id", name="uq_telegram_account_links_tg_user_id"),
    )
    op.create_index("ix_telegram_account_links_tg_user_id", "telegram_account_links", ["tg_user_id"])
    op.create_index("ix_telegram_account_links_alias", "telegram_account_links", ["alias"])

    op.create_table(
        "telegram_login_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_tg_user_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_telegram_login_tokens_token"),
    )
    op.create_index("ix_telegram_login_tokens_token", "telegram_login_tokens", ["token"])

    op.create_table(
        "telegram_poller_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("telegram_poller_state")
    op.drop_index("ix_telegram_login_tokens_token", table_name="telegram_login_tokens")
    op.drop_table("telegram_login_tokens")
    op.drop_index("ix_telegram_account_links_alias", table_name="telegram_account_links")
    op.drop_index("ix_telegram_account_links_tg_user_id", table_name="telegram_account_links")
    op.drop_table("telegram_account_links")
    op.drop_index("ix_telegram_messages_tg_user_id", table_name="telegram_messages")
    op.drop_index("ix_telegram_messages_window", table_name="telegram_messages")
    op.drop_table("telegram_messages")
    # Значение enum file_purpose.TELEGRAM_INGEST остаётся: в Postgres нет
    # DROP VALUE (как и в 5b3615d19b4e).
