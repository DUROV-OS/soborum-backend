import base64
from datetime import date
from decimal import Decimal
from io import BytesIO
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Money = Annotated[Decimal, Field(ge=0, le=Decimal("999999999999"), max_digits=20, decimal_places=8, allow_inf_nan=False)]
Quantity = Annotated[Decimal, Field(gt=0, le=1000000, max_digits=16, decimal_places=6, allow_inf_nan=False)]
Percent = Annotated[Decimal, Field(ge=0, lt=100, max_digits=7, decimal_places=4, allow_inf_nan=False)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def image_value(value):
    if not value:
        return ""
    from PIL import Image
    try:
        prefix, data = value.split(",", 1)
        if prefix not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64", "data:image/gif;base64"}:
            raise ValueError()
        raw = base64.b64decode(data, validate=True)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError()
        with Image.open(BytesIO(raw)) as img:
            if img.width * img.height > 16000000:
                raise ValueError()
            img.verify()
    except Exception as exc:
        raise ValueError("Нужно изображение PNG/JPG/WEBP/GIF до 5 МБ и 16 млн пикселей") from exc
    return value


class Profile(Strict):
    name: str = Field(min_length=1, max_length=120)
    margin: Percent = Decimal(35)
    labor: Percent = Decimal(10)
    management: Percent = Decimal(3)
    tax: Percent = Decimal(6)

    @model_validator(mode="after")
    def feasible(self):
        if self.margin + self.labor + self.management + self.tax >= 100:
            raise ValueError("Сумма маржи и расходов от выручки должна быть меньше 100%")
        return self


class SettingsData(Strict):
    company: str = Field("Durov.House", min_length=1, max_length=255)
    logo: str = Field("", max_length=7100000)
    profiles: list[Profile] = Field(default_factory=lambda: [Profile(name="Базовый")], min_length=1, max_length=50)
    labor_label: str = Field("Оплата труда", min_length=1, max_length=120)
    management_label: str = Field("Менеджмент", min_length=1, max_length=120)
    tax_label: str = Field("Налоги", min_length=1, max_length=120)
    rate_step: Money = Decimal(500)
    total_step: Money = Decimal(1000)
    addon_step: Money = Decimal(100)
    mode: Literal["area", "unit"] = "area"

    _image = field_validator("logo")(image_value)

    @model_validator(mode="after")
    def unique(self):
        if len({p.name.casefold() for p in self.profiles}) != len(self.profiles):
            raise ValueError("Названия профилей должны быть уникальными")
        if min(self.rate_step, self.total_step, self.addon_step) <= 0:
            raise ValueError("Шаг округления должен быть положительным")
        return self


class SettingsInput(Strict):
    version: int = Field(ge=0)
    content: SettingsData


class MaterialInput(Strict):
    version: int | None = Field(None, ge=1)
    warehouse_material_id: int | None = Field(None, gt=0)
    name: str = Field(min_length=1, max_length=255)
    category: str = Field("", max_length=120)
    unit: str = Field(min_length=1, max_length=32)
    price: Money | None = None
    price_source: str = Field("", max_length=1000)
    effective_on: date
    archived: bool = False


class MaterialLine(Strict):
    material_id: int = Field(gt=0)
    quantity: Quantity
    pieces: int = Field(1, ge=1, le=1000000)
    price: Money | None = None
    price_reason: str = Field("", max_length=500)

    @model_validator(mode="after")
    def explicit_price(self):
        if self.price is not None and not self.price_reason:
            raise ValueError("Укажите основание ручной цены строки")
        return self


class PackageLine(Strict):
    addon_id: int = Field(gt=0)
    quantity: Quantity


class Expenses(Strict):
    labor: Money | None = None
    transport: Money | None = None
    subcontract: Money | None = None
    overhead: Money | None = None
    commercial: Money | None = None
    advertising: Money | None = None
    taxes: Money | None = None
    reserve: Money | None = None
    evidence: str = Field("", max_length=1000)


class ModuleGeometry(Strict):
    length: Quantity
    width: Quantity
    count: int = Field(1, ge=1, le=1000)


class ItemData(Strict):
    source_recipe_id: int | None = Field(None, gt=0)
    source_recipe_version: int | None = Field(None, gt=0)
    warehouse_product_id: int | None = Field(None, gt=0)
    house_model_key: str | None = Field(None, max_length=64)
    code: str = Field("", max_length=120)
    category: str = Field("", max_length=120)
    model: str = Field("", max_length=120)
    profile: str = Field("Базовый", min_length=1, max_length=120)
    mode: Literal["area", "unit"] = "unit"
    unit: str = Field("шт", min_length=1, max_length=32)
    area: Quantity | None = None
    length: Quantity | None = None
    width: Quantity | None = None
    modules: list[ModuleGeometry] = Field(default_factory=list, max_length=100)
    image: str = Field("", max_length=7100000)
    note: str = Field("", max_length=4000)
    lines: list[MaterialLine] = Field(default_factory=list, max_length=1000)
    replacements: list[MaterialLine] = Field(default_factory=list, max_length=1000)
    expenses: Expenses = Field(default_factory=Expenses)
    margin: Percent | None = None
    retail_price: Money | None = None
    retail_rate: Money | None = None
    addons: list[PackageLine] = Field(default_factory=list, max_length=100)
    package_prices: dict[str, Money] = Field(default_factory=dict, max_length=100)
    discount: Percent = Decimal(0)

    _image = field_validator("image")(image_value)

    @model_validator(mode="after")
    def unique(self):
        for rows, key in ((self.lines, "material_id"), (self.replacements, "material_id"), (self.addons, "addon_id")):
            if len({getattr(r, key) for r in rows}) != len(rows):
                raise ValueError("Объедините повторяющиеся позиции в одну строку")
        return self


class ItemInput(Strict):
    version: int | None = Field(None, ge=1)
    kind: Literal["template", "addon", "package"]
    title: str = Field(min_length=1, max_length=255)
    archived: bool = False
    content: ItemData


class Selection(Strict):
    item_id: int = Field(gt=0)
    quantity: Quantity = Decimal(1)


class EstimateInput(Strict):
    request_id: UUID
    version: int | None = Field(None, ge=1)
    title: str = Field(min_length=1, max_length=255)
    number: str = Field("", max_length=120)
    client_id: int | None = Field(None, gt=0)
    crm_deal_id: int | None = Field(None, gt=0)
    sales_order_id: int | None = Field(None, gt=0)
    sales_order_line: int | None = Field(None, ge=0, le=499)
    template_id: int = Field(gt=0)
    quantity: Quantity = Decimal(1)
    selections: list[Selection] = Field(default_factory=list, max_length=100)
    discount: Percent = Decimal(0)
    sale_price: Money | None = None
    discount_amount: Money | None = None
    lines: list[MaterialLine] | None = Field(None, max_length=1000)
    profile: Profile | None = None
    expenses: Expenses | None = None
    note: str = Field("", max_length=4000)
    image: str | None = Field(None, max_length=7100000)
    reason: str = Field("Изменение сметы", min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_lines(self):
        if self.lines is not None and len({l.material_id for l in self.lines}) != len(self.lines):
            raise ValueError("Объедините повторяющиеся материалы в одну строку")
        return self

    @field_validator("image")
    @classmethod
    def image_valid(cls, value):
        return image_value(value) if value is not None else None


class VersionAction(Strict):
    version: int = Field(ge=1)
    request_id: UUID
    reason: str = Field(min_length=1, max_length=500)


class PriceVersionInput(Strict):
    title: str = Field(min_length=1, max_length=255)


class ArchiveAction(VersionAction):
    archived: bool


class PriceRestore(Strict):
    versions: dict[int, int] = Field(max_length=1000)
    reason: str = Field(min_length=1, max_length=500)


class PriceChange(Strict):
    id: int = Field(gt=0)
    version: int = Field(ge=1)
    price: Money | None = None
    rate: Money | None = None


class PriceChanges(Strict):
    items: list[PriceChange] = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)
