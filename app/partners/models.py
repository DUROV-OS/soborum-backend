"""База партнёров (0083-a).

Партнёр — тот, с кем компания договаривается о сотрудничестве и кто приводит
клиентов: коммерция, агентства недвижимости, риэлторы, специалисты по земле.
Это не клиент (у него нет стадий и цикла) и не контрагент бухгалтерии (тот —
плательщик/получатель денег, 0081-c), поэтому отдельная таблица.
"""

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PartnerCategory(str, enum.Enum):
    COMMERCE = "COMMERCE"  # коммерция
    REAL_ESTATE_AGENCY = "REAL_ESTATE_AGENCY"  # агентство недвижимости
    REALTOR = "REALTOR"  # риэлтор
    LAND_SPECIALIST = "LAND_SPECIALIST"  # специалист по земле


class Partner(Base):
    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[PartnerCategory] = mapped_column(
        Enum(PartnerCategory, name="partner_category"), nullable=False
    )
    # ФИО человека или название организации — как партнёра называют в работе.
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Город обязателен: заказчику важно видеть, откуда партнёр.
    city: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # У риэлтора — агентство, в котором он работает; у остальных — по желанию.
    organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Тот же формат, что Client.contacts: [{"messenger": "telegram", "contact": "@ivan"}].
    contacts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    notes: Mapped[list["PartnerNote"]] = relationship(
        back_populates="partner", cascade="all, delete-orphan", order_by="PartnerNote.id.desc()"
    )


class PartnerNote(Base):
    """Договорённости и условия с партнёром, пока нет общей ленты переписки (0083-d)."""

    __tablename__ = "partner_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    partner: Mapped["Partner"] = relationship(back_populates="notes")
