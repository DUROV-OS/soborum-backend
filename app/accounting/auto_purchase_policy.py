"""Pure, fail-closed policy for later integration with verified purchase facts.

This does not read a database, approve an order or execute a payment. Callers
must obtain every input from authenticated, verified records in one transaction.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ComparablePurchase:
    supplier_id: int
    item_id: int
    unit: str
    currency: str
    unit_price: Decimal
    confirmed: bool
    evidence_id: int | None


@dataclass(frozen=True)
class PurchaseLine:
    current: ComparablePurchase
    previous: ComparablePurchase | None
    within_techcard_quantity: bool


@dataclass(frozen=True)
class PurchaseFacts:
    supplier_id: int
    trusted_supplier: bool
    confirmed_previous_supplier_order: bool
    approved_sale: bool
    active_techcard: bool
    techcard_matches_sale: bool
    source_documents_confirmed: bool
    lines: tuple[PurchaseLine, ...]


def auto_approval_reasons(facts: PurchaseFacts) -> list[str]:
    reasons: list[str] = []
    if not facts.trusted_supplier: reasons.append('supplier_not_verified')
    if not facts.confirmed_previous_supplier_order: reasons.append('no_previous_supplier_order')
    if not facts.approved_sale: reasons.append('sale_not_approved')
    if not facts.active_techcard or not facts.techcard_matches_sale: reasons.append('techcard_not_valid_for_sale')
    if not facts.source_documents_confirmed: reasons.append('documents_not_confirmed')
    if not facts.lines: reasons.append('no_items')
    for line in facts.lines:
        current, previous = line.current, line.previous
        if not line.within_techcard_quantity: reasons.append('quantity_exceeds_techcard')
        if current.supplier_id != facts.supplier_id or not current.confirmed or not current.evidence_id:
            reasons.append('current_price_not_confirmed')
        if not current.unit or not current.currency or not current.unit_price.is_finite() or current.unit_price <= 0:
            reasons.append('invalid_current_price')
        if previous is None:
            reasons.append('no_comparable_previous_price')
            continue
        if (not previous.confirmed or not previous.evidence_id or not previous.unit_price.is_finite()
                or previous.unit_price <= 0):
            reasons.append('previous_price_not_confirmed')
            continue
        if (previous.supplier_id, previous.item_id, previous.unit, previous.currency) != (
            current.supplier_id, current.item_id, current.unit, current.currency
        ):
            reasons.append('prices_not_comparable')
        elif current.unit_price.is_finite() and current.unit_price > previous.unit_price:
            reasons.append('price_increased')
    return list(dict.fromkeys(reasons))
