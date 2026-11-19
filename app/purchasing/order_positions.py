"""Amounts and live warehouse quantities for a supplier order card."""

from decimal import Decimal, InvalidOperation

from app.moysklad.importer import ref, rows
from app.moysklad.models import SourceRecord
from app.purchasing import controls
from app.purchasing.importer import imported_unit_price
from app.purchasing.service import dec
from app.warehouse.models import WarehouseMaterial
from app.warehouse.reservation_service import allocations


def source_tax(item, source, source_positions, index):
    if not source or index >= len(source_positions):
        return None
    raw = source_positions[index]
    assortment = raw.get('assortment') or {}
    if (item['material'] != str(assortment.get('name') or ref(assortment))
            or dec(item['quantity']) != dec(raw.get('quantity'))
            or dec(item['unit_price']) != dec(float(imported_unit_price(raw, source)))):
        return None
    if source.get('vatEnabled') is False or raw.get('vatEnabled') is False:
        return 'exempt', Decimal(0)
    if source.get('vatEnabled') is True and raw.get('vat') is not None:
        try:
            rate = Decimal(str(raw['vat']))
            if rate.is_finite() and 0 <= rate <= 100:
                return 'included', rate
        except InvalidOperation:
            pass
    return None


def positions(db, order):
    ids = {item['warehouse_material_id'] for item in order.items if item.get('warehouse_material_id')}
    materials = {m.id: m for m in db.query(WarehouseMaterial).filter(WarehouseMaterial.id.in_(ids))} if ids else {}
    reserved = allocations(db, ids) if ids else {}
    original = (db.query(SourceRecord).filter_by(entity_type='purchaseorder', source_id=order.source_id).first()
                if order.source_id else None)
    source = original.payload if original else {}
    source_positions = rows(source.get('positions'))
    event, valid_price = controls.price_state(db, order)
    terms = {line['index']: line for line in event.content['request']['lines']} if valid_price else {}
    result = []
    for index, item in enumerate(order.items):
        material = materials.get(item.get('warehouse_material_id')) if item.get('stock_tracked', True) else None
        stock = dec(material.quantity_in_stock) if material else None
        free = max(Decimal(0), stock - sum(reserved.get(material.id, {}).values())) if material else None
        known = ((terms[index]['vat'], dec(terms[index]['vat_rate'])) if index in terms
                 else source_tax(item, source, source_positions, index))
        tax, rate = known if known else (None, None)
        gross = (dec(item['quantity']) * dec(item['unit_price'])).quantize(Decimal('.01'))
        net = gross / (1 + rate / 100) if tax == 'included' else gross
        result.append({
            'number': index + 1, 'name': item['material'], 'quantity': item['quantity'], 'unit': item.get('unit', ''),
            'received_quantity': item.get('received_quantity', 0),
            'stock_unit': material.unit if material else None,
            'stock': str(stock) if stock is not None else None,
            'available': str(free) if free is not None else None,
            'vat': tax, 'vat_rate': format(rate.normalize(), 'f') if rate is not None else None,
            'unit_price': item['unit_price'],
            'amount_with_vat': f'{gross:.2f}' if known else None,
            'amount': f'{net:.2f}',
        })
    return result
