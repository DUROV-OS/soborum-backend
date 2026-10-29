from dataclasses import replace
from decimal import Decimal
from app.accounting.auto_purchase_policy import ComparablePurchase, PurchaseLine, PurchaseFacts, auto_approval_reasons


def facts():
    previous = ComparablePurchase(1, 2, 'шт', 'RUB', Decimal('100.00'), True, 10)
    current = replace(previous, unit_price=Decimal('100.00'), evidence_id=11)
    return PurchaseFacts(1, True, True, True, True, True, True, (PurchaseLine(current, previous, True),))


def test_equal_and_lower_prices_can_be_eligible():
    f = facts()
    assert auto_approval_reasons(f) == []
    line = f.lines[0]
    assert auto_approval_reasons(replace(f, lines=(replace(line, current=replace(line.current, unit_price=Decimal('99.99'))),))) == []


def test_unknown_price_increase_and_mismatched_units_are_rejected():
    f = facts(); line = f.lines[0]
    assert 'price_increased' in auto_approval_reasons(replace(f, lines=(replace(line, current=replace(line.current, unit_price=Decimal('100.01'))),)))
    assert 'no_comparable_previous_price' in auto_approval_reasons(replace(f, lines=(replace(line, previous=None),)))
    assert 'prices_not_comparable' in auto_approval_reasons(replace(f, lines=(replace(line, current=replace(line.current, unit='м')),)))
    assert 'previous_price_not_confirmed' in auto_approval_reasons(replace(f, lines=(replace(line, previous=replace(line.previous, evidence_id=None)),)))


def test_supplier_sale_techcard_and_quantities_are_required():
    f = facts()
    assert 'supplier_not_verified' in auto_approval_reasons(replace(f, trusted_supplier=False))
    assert 'no_previous_supplier_order' in auto_approval_reasons(replace(f, confirmed_previous_supplier_order=False))
    assert 'sale_not_approved' in auto_approval_reasons(replace(f, approved_sale=False))
    assert 'techcard_not_valid_for_sale' in auto_approval_reasons(replace(f, techcard_matches_sale=False))
    assert 'quantity_exceeds_techcard' in auto_approval_reasons(replace(f, lines=(replace(f.lines[0], within_techcard_quantity=False),)))
