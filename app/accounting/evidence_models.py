"""Documentary review supplements payments; it never rewrites a posted movement."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class PaymentEvidenceDocument(Base):
    __tablename__ = 'payment_evidence_documents'
    id: Mapped[int] = mapped_column(primary_key=True)
    movement_id: Mapped[int] = mapped_column(ForeignKey('money_movements.id'), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey('file_assets.id'), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentEvidenceVersion(Base):
    __tablename__ = 'payment_evidence_versions'
    __table_args__ = (UniqueConstraint('movement_id', 'version', name='uq_payment_evidence_version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    movement_id: Mapped[int] = mapped_column(ForeignKey('money_movements.id'), index=True)
    version: Mapped[int] = mapped_column()
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentEvidenceCheck(Base):
    __tablename__ = 'payment_evidence_checks'
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey('payment_evidence_versions.id'), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
