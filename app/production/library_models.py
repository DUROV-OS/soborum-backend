from datetime import datetime
from sqlalchemy import String, JSON, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class HouseTechnicalCard(Base):
    __tablename__ = "house_technical_cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[int] = mapped_column(default=1)
    content: Mapped[dict] = mapped_column(JSON)
    source_hash: Mapped[str] = mapped_column(String(64))
    manually_edited: Mapped[bool] = mapped_column(default=False)
    updated_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


