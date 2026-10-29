"""Lossless read replica of the owner's MoySklad export. Never creates logins."""

from datetime import datetime
from sqlalchemy import Boolean, DateTime, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class SourceRecord(Base):
    __tablename__ = "moysklad_records"
    __table_args__ = (UniqueConstraint("entity_type", "source_id", name="uq_moysklad_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(100), index=True)
    source_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(1000))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    state: Mapped[str | None] = mapped_column(String(255), nullable=True)
    search_text: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceLink(Base):
    __tablename__ = "moysklad_links"
    __table_args__ = (UniqueConstraint("source_key", "target_type", name="uq_moysklad_target"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(500))
    target_type: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[int] = mapped_column()


class ImportRun(Base):
    __tablename__ = "moysklad_imports"
    id: Mapped[int] = mapped_column(primary_key=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    summary: Mapped[dict] = mapped_column(JSON)
