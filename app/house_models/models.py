import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class HouseModelKind(str, enum.Enum):
    CATALOG = "catalog"
    INDIVIDUAL = "individual"


class HouseModelConfirmation(str, enum.Enum):
    """Грубая (для бейджа в списке) степень подтверждения реальным
    производственным/сделочным опытом — как в сводной таблице MOC_Models
    источника. Точная формулировка источника — в `confirmation_label`."""

    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    NONE = "none"


class HouseModelCard(Base):
    """Справочная карточка типового проекта дома (задача 0043-a) — витрина,
    почти не CRUD-сущность: подавляющее большинство полей пишется только
    разовым импортом из базы знаний (`app.house_models.import_kb`).

    Единственное исключение (задача 0073-b) — `typical_ar_file_id`/
    `typical_kr_file_id`: правятся через `PATCH /catalog/{key}/typical-documents`,
    доступно только администратору. Все остальные поля по-прежнему
    только-импорт, без POST/PATCH/DELETE.

    Только всегда-единообразные поля структурированы (площадь/цена/статус
    подтверждения); остальные секции карточки — markdown-текст по одному
    полю на секцию, т.к. у карточек сильно разный набор и глубина данных."""

    __tablename__ = "house_model_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[HouseModelKind] = mapped_column(Enum(HouseModelKind, name="house_model_kind"), nullable=False)
    series: Mapped[str | None] = mapped_column(String(32), nullable=True)

    area_footprint_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    area_total_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_site_rub: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deal_amount_rub: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    confirmation: Mapped[HouseModelConfirmation] = mapped_column(
        Enum(HouseModelConfirmation, name="house_model_confirmation"), nullable=False
    )
    confirmation_label: Mapped[str] = mapped_column(String(255), nullable=False)

    source_note_path: Mapped[str] = mapped_column(String(255), nullable=False)

    # NULL там, где на сайте durov.house нет собственной страницы модели
    # (индивидуальные проекты, barn-dh83) — см. 0043-c, никогда не выдумывается.
    planning_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_assets.id"), nullable=True
    )
    planning_image: Mapped["FileAsset | None"] = relationship(
        foreign_keys=[planning_image_id]
    )  # noqa: F821

    # Типовые АР/КР (задача 0073-b) — единственное исключение из read-only:
    # образец для модели целиком, правится только через
    # PATCH /catalog/{key}/typical-documents (require_admin), никогда через
    # import_kb. Намеренно не участвует в генерации графа этапов
    # (app.production.stage_template_service читает только Client.kr_file
    # конкретного клиента).
    typical_ar_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_assets.id"), nullable=True
    )
    typical_ar: Mapped["FileAsset | None"] = relationship(
        foreign_keys=[typical_ar_file_id]
    )  # noqa: F821
    typical_kr_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_assets.id"), nullable=True
    )
    typical_kr: Mapped["FileAsset | None"] = relationship(
        foreign_keys=[typical_kr_file_id]
    )  # noqa: F821

    characteristics_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    planning_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    configurations_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    modules_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    economics_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    production_experience_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    deals_without_pz_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    files_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_questions_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes_md: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
