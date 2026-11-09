"""Sales feedback is append-only and never inferred from a button click."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class ConversionReview(Base):
    __tablename__ = 'marketing_conversion_reviews'
    __table_args__ = (UniqueConstraint('request_id', 'version', name='uq_conversion_review_version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey('client_lead_requests.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(1000))
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
