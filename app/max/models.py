"""Чаты и сообщения MAX-бота (0082).

У Bot API нет метода списка чатов, а историю группы отдаёт только если бот в
ней администратор — поэтому всё, что бот видел, храним у себя. Наполняется из
событий (webhook / long polling, см. app/max/ingest.py) и из собственных
отправок бота. id чатов и пользователей MAX — BigInteger (бывают
отрицательными и больше int32), mid сообщения — строка.
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, Session, mapped_column, object_session

from app.db.base import Base


class MaxBotChat(Base):
    __tablename__ = "max_bot_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # dialog | chat | channel — как в Bot API
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="dialog")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # для диалога — собеседник бота
    dialog_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # active | removed (бота удалили из группы / пользователь остановил бота)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_event_time: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # мс
    unread: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MaxBotMessage(Base):
    __tablename__ = "max_bot_messages"

    mid: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_outgoing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # вложения ровно как пришли от MAX (type + payload + ...), приводятся к
    # форме фронта при выдаче
    attachments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)  # мс
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


def bot_knows_chat(obj, chat_id: int | None) -> bool:
    """Видел ли бот чат ``chat_id`` (есть строка в max_bot_chats). Привязки
    клиентов и поставщиков, сделанные до 0082, хранят id чатов
    пользовательского аккаунта — бот их не знает, лента по ним отдаёт 404.
    ``obj`` — ORM-объект привязки: берём его сессию."""
    if chat_id is None:
        return False
    session: Session | None = object_session(obj)
    return session is not None and session.get(MaxBotChat, chat_id) is not None
