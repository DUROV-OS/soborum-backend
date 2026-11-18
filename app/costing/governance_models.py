from datetime import date, datetime
from sqlalchemy import Date, DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class CostingApprovalPolicy(Base):
    __tablename__ = 'costing_approval_policy'
    id: Mapped[int] = mapped_column(primary_key=True)
    production_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    accounting_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    director_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))


class CostingSignoff(Base):
    __tablename__ = 'costing_signoffs'
    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[int] = mapped_column(index=True)
    stage: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column()
    revision_id: Mapped[int | None] = mapped_column(ForeignKey('costing_revisions.id'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostingOverheadPlan(Base):
    __tablename__ = 'costing_overhead_plans'
    __table_args__ = (UniqueConstraint('month', 'version', name='uq_costing_month_version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[date] = mapped_column(Date, index=True)
    version: Mapped[int] = mapped_column()
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostingEvidenceDocument(Base):
    __tablename__ = 'costing_evidence_documents'
    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[int] = mapped_column(index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey('file_assets.id'), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    title: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
