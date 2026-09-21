"""Шаблон графа этапов производства (0066-d) — предложение ИИ по КР клиента,
на модель дома (`house_model_key`) или разовое для индивидуального проекта.

Проходит статусный цикл `draft → reviewed → confirmed` (по образцу других
ИИ-предложений проекта): человек видит блоки/задачи/материалы рядом с
цитатами на страницы КР, правит и подтверждает, прежде чем шаблон применяется
к реальному производству ([[0066-f]]).
"""

import enum
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, JSON, String, Table, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.production.models import MappingConfidence


class TemplateStatus(str, enum.Enum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    CONFIRMED = "confirmed"


class ProductionStageTemplate(Base):
    __tablename__ = "production_stage_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL — индивидуальный проект: шаблон одноразовый, не переиспользуется.
    house_model_key: Mapped[str | None] = mapped_column(ForeignKey("house_model_cards.key"), nullable=True)
    status: Mapped[TemplateStatus] = mapped_column(
        Enum(TemplateStatus, name="production_stage_template_status"),
        nullable=False,
        default=TemplateStatus.DRAFT,
    )
    source_client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    blocks: Mapped[list["TemplateBlock"]] = relationship(
        back_populates="template", cascade="all, delete-orphan", order_by="TemplateBlock.sequence"
    )
    source_client: Mapped["Client"] = relationship()  # noqa: F821
    confirmed_by: Mapped["User | None"] = relationship()  # noqa: F821


template_block_dependencies = Table(
    "template_block_dependencies",
    Base.metadata,
    Column("block_id", ForeignKey("template_blocks.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("template_blocks.id", ondelete="CASCADE"), primary_key=True),
)


class TemplateBlock(Base):
    __tablename__ = "template_blocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("production_stage_templates.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # [{"page_number": int, "note": str | None}, ...]
    kr_page_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    template: Mapped["ProductionStageTemplate"] = relationship(back_populates="blocks")
    tasks: Mapped[list["TemplateBlockTask"]] = relationship(back_populates="block", cascade="all, delete-orphan")
    materials: Mapped[list["TemplateBlockMaterial"]] = relationship(
        back_populates="block", cascade="all, delete-orphan"
    )

    # Блоки шаблона, от которых зависит этот блок — тот же паттерн, что
    # `block_dependencies` у реальных `ProductionBlock` (app/production/models.py).
    depends_on: Mapped[list["TemplateBlock"]] = relationship(
        secondary=template_block_dependencies,
        primaryjoin=id == template_block_dependencies.c.block_id,
        secondaryjoin=id == template_block_dependencies.c.depends_on_id,
        backref="blocks",
    )

    @property
    def depends_on_ids(self) -> list[int]:
        return [b.id for b in self.depends_on]


class TemplateBlockTask(Base):
    __tablename__ = "template_block_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_block_id: Mapped[int] = mapped_column(
        ForeignKey("template_blocks.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # {"page_number": int, "note": str | None} — nullable: ИИ не обязан искать
    # страницу для каждой составляющей, если её в тексте нет.
    kr_page_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    block: Mapped["TemplateBlock"] = relationship(back_populates="tasks")


class TemplateBlockMaterial(Base):
    __tablename__ = "template_block_materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_block_id: Mapped[int] = mapped_column(
        ForeignKey("template_blocks.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    kr_page_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Не всегда однозначно сопоставляется с каталогом при генерации —
    # донаполняется инженером на проверке ([[0066-e]]). С 0073-a заполняется
    # автоматически при генерации через app.production.material_matching
    # (см. stage_template_service._persist_draft); ручная правка на проверке
    # ([[0066-e]]) продолжает работать поверх этого поля как раньше.
    warehouse_material_id: Mapped[int | None] = mapped_column(ForeignKey("warehouse_materials.id"), nullable=True)
    # Уверенность автосопоставления (0073-a): NULL — сопоставлено вручную или
    # не сопоставлено вовсе; HIGH/MEDIUM — предложено ИИ (см.
    # app.production.material_matching.MatchResult). MEDIUM показывается на
    # проверке шаблона как «требует проверки».
    confidence: Mapped[MappingConfidence | None] = mapped_column(
        Enum(MappingConfidence, name="mapping_confidence"), nullable=True
    )

    block: Mapped["TemplateBlock"] = relationship(back_populates="materials")
    warehouse_material: Mapped["WarehouseMaterial | None"] = relationship()  # noqa: F821

    @property
    def warehouse_material_title(self) -> str | None:
        return self.warehouse_material.title if self.warehouse_material_id else None
