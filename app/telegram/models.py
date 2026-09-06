"""Telegram bridge tables.

Three concerns, three tables, plus a one-row poller cursor:

* ``TelegramMessage`` — every group message the bot sees is archived here as
  it arrives (poller / webhook). The daily ingest job (app/telegram/ingest.py)
  then reads a 24-hour window out of this table and hands it to the KB agent;
  ``ingested_into_kb_at`` marks the ones already written so a re-run is safe.
* ``TelegramAccountLink`` — "алиас → сотрудник Durov-OS": maps a Telegram
  user to a ``users`` row. Filled either by the deep-link login flow or by
  hand from the static seed in app/telegram/aliases.py. This is what lets a
  future feature attribute a chat message to a real employee.
* ``TelegramLoginToken`` — one-shot token behind the "войти через Telegram"
  deep link (``t.me/<bot>?start=login_<token>``). The bot consumes it on
  ``/start`` and binds the Telegram account to the issuing user.
* ``TelegramPollerState`` — singleton (id=1) holding the last processed
  ``update_id`` so long-polling resumes where it left off across restarts.

Nothing here is scoped to one chat on purpose beyond ``chat_id`` being stored:
the system currently watches a single group (``settings.telegram_chat_id``),
but the schema already carries ``chat_id`` everywhere so watching several
groups later is a query change, not a migration.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_telegram_chat_message"),
        Index("ix_telegram_messages_window", "chat_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    tg_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # "text" | "photo" | "document" | "other" — enough to decide how to render
    # the entry and whether there is a file to pull.
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)  # body or caption

    file_unique_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_assets.id", ondelete="SET NULL"), nullable=True
    )

    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_into_kb_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    file_asset = relationship("FileAsset")


class TelegramAccountLink(Base):
    __tablename__ = "telegram_account_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Nullable: a link row can exist as a pure alias placeholder before it is
    # bound to a real account. SET NULL keeps the alias if the user is deleted.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    alias: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship()  # noqa: F821


class TelegramLoginToken(Base):
    __tablename__ = "telegram_login_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_tg_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship()  # noqa: F821


class TelegramPollerState(Base):
    """Single row, id=1. ``last_update_id`` is the highest Telegram update_id
    we have acknowledged; the poller asks for ``offset = last_update_id + 1``."""

    __tablename__ = "telegram_poller_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_update_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
