from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pydantic import BaseModel, ConfigDict, Field

MARGIN_PERCENT = Decimal('35')
MONEY = Decimal('0.01')


class MarginPreviewInput(BaseModel):
    """Inputs for one item's provisional sale price, in rubles.

    Null means unknown. Zero is permitted only when explicitly entered: e.g.
    a purchased item might not require separate production labour.
    """
    model_config = ConfigDict(extra='forbid')
    sale_price: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    materials: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    labor: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    purchase: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    other_variable: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    fixed_per_month: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    planned_items: int | None = Field(default=None, gt=0, le=1000000)


def margin_preview(data: MarginPreviewInput) -> dict:
    labels = {'sale_price': 'Цена продажи', 'materials': 'Материалы', 'labor': 'Работы',
              'purchase': 'Закупка', 'other_variable': 'Прочие переменные расходы',
              'fixed_per_month': 'Постоянные расходы за месяц', 'planned_items': 'План изделий за месяц'}
    missing = [label for key, label in labels.items() if getattr(data, key) is None]
    if missing:
        return {'status': 'incomplete', 'missing': missing, 'target_margin_percent': '35',
                'cost': None, 'fixed_share': None, 'profit': None, 'margin_percent': None,
                'markup_percent': None, 'minimum_price': None}
    fixed_share = data.fixed_per_month / data.planned_items
    cost = data.materials + data.labor + data.purchase + data.other_variable + fixed_share
    profit = data.sale_price - cost
    minimum = (cost / Decimal('0.65')).quantize(MONEY, rounding=ROUND_CEILING)
    margin = profit / data.sale_price * 100 if data.sale_price > 0 else None
    markup = profit / cost * 100 if cost > 0 else None
    def display(value):
        return str(value.quantize(MONEY, rounding=ROUND_HALF_UP)) if value is not None else None
    # Compare before rounding; 34.999% must not pass as 35.00%.
    passed = data.sale_price > 0 and data.sale_price * Decimal('0.65') >= cost
    return {'status': 'meets_target' if passed else 'below_target', 'missing': [], 'target_margin_percent': '35',
            'cost': display(cost), 'fixed_share': display(fixed_share), 'profit': display(profit),
            'margin_percent': display(margin), 'markup_percent': display(markup), 'minimum_price': str(minimum)}
