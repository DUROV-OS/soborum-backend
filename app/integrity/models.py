from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IntegrityEvent(Base):
    __tablename__ = "integrity_events"

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    entity: Mapped[str] = mapped_column(String(128), index=True)
    entity_key: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text)
    previous_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
