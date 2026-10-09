"""Своё название чата MAX в нашей системе (0106) — не трогает имя/название
в самом MAX, только то, что показываем в «Все чаты» и в шапке диалога.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MaxChatTitle(Base):
    __tablename__ = "max_chat_titles"
    __table_args__ = (UniqueConstraint("max_chat_id", name="uq_max_chat_titles_max_chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    max_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
