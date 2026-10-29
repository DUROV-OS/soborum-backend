from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pydantic import BaseModel, ConfigDict, Field

MARGIN_PERCENT = Decimal('35')
MONEY = Decimal('0.01')


class MarginPreviewInput(BaseModel):
    """Advisory, per-item values; missing is unknown, never assumed zero.

    For this preview both commission percentages use the sale price after
    discounts as their base. Contract-specific bases need separate evidence.
    """
    model_config = ConfigDict(extra='forbid')
    sale_price: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    materials: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    labor: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    purchase: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    other_variable: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    advertising: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    realtor_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=7, decimal_places=4, allow_inf_nan=False)
    salesperson_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=7, decimal_places=4, allow_inf_nan=False)
    fixed_per_month: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)
    planned_items: int | None = Field(default=None, gt=0, le=1000000)


def margin_preview(data: MarginPreviewInput) -> dict:
    labels = {'sale_price': 'Цена продажи', 'materials': 'Материалы', 'labor': 'Работы',
              'purchase': 'Закупка', 'other_variable': 'Прочие переменные расходы',
              'advertising': 'Реклама на изделие', 'realtor_percent': 'Процент риелтора',
              'salesperson_percent': 'Процент продавца', 'fixed_per_month': 'Постоянные расходы за месяц',
              'planned_items': 'План изделий за месяц'}
    missing = [label for key, label in labels.items() if getattr(data, key) is None]
    if missing:
        return {'status': 'incomplete', 'missing': missing, 'target_margin_percent': '35',
                'cost': None, 'fixed_share': None, 'realtor_fee': None, 'salesperson_fee': None,
                'profit': None, 'margin_percent': None, 'markup_percent': None, 'minimum_price': None}
    fixed_share = data.fixed_per_month / data.planned_items
    base_cost = data.materials + data.labor + data.purchase + data.other_variable + data.advertising + fixed_share
    realtor_fee = data.sale_price * data.realtor_percent / 100
    salesperson_fee = data.sale_price * data.salesperson_percent / 100
    cost = base_cost + realtor_fee + salesperson_fee
    profit = data.sale_price - cost
    available = Decimal('0.65') - (data.realtor_percent + data.salesperson_percent) / 100
    feasible = available > 0 or (available == 0 and base_cost == 0)
    minimum = ((base_cost / available).quantize(MONEY, rounding=ROUND_CEILING) if available > 0
               else MONEY if feasible else None)
    margin = profit / data.sale_price * 100 if data.sale_price > 0 else None
    markup = profit / cost * 100 if cost > 0 else None
    def display(value):
        return str(value.quantize(MONEY, rounding=ROUND_HALF_UP)) if value is not None else None
    # Check the unrounded amounts; 34.999% must not pass as 35.00%.
    passed = data.sale_price > 0 and data.sale_price * Decimal('0.65') >= cost
    return {'status': 'infeasible' if not feasible else 'meets_target' if passed else 'below_target',
            'missing': [], 'target_margin_percent': '35', 'cost': display(cost), 'fixed_share': display(fixed_share),
            'realtor_fee': display(realtor_fee), 'salesperson_fee': display(salesperson_fee), 'profit': display(profit),
            'margin_percent': display(margin), 'markup_percent': display(markup), 'minimum_price': display(minimum)}
