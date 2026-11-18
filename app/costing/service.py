"""Server-owned arithmetic. Monetary inputs and stored snapshots use decimal strings."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from app.common.module_access import AccessLevel, Module
from app.costing.models import CostingAudit, CostingEstimate, CostingItem, CostingMaterial, CostingRevision, CostingSettings
from app.costing.schemas import EstimateInput, ItemData, Profile, SettingsData
from app.users.models import UserRole
from app.warehouse.models import WarehouseMaterial

D = Decimal
ZERO = D(0)
EXPENSE_LABELS = {"labor": "Работы фиксированно", "transport": "Транспорт", "subcontract": "Подряд",
                  "overhead": "Накладные на изделие", "commercial": "Коммерческие расходы",
                  "advertising": "Реклама", "taxes": "Налоги фиксированно", "reserve": "Резерв"}


def fail(message, status=422):
    raise HTTPException(status, message)


def decimal(value):
    return D(str(value))


def money(value):
    return str(value.quantize(D("0.01"), rounding=ROUND_HALF_UP)) if value is not None else None


def rounded(value, step):
    step = decimal(step)
    return (value / step).quantize(D(1), rounding=ROUND_HALF_UP) * step


def cost_access(user, level=AccessLevel.VIEW):
    # A sales job never implicitly gains engineering/financial access.
    return user.role == UserRole.ADMIN or (user.job_role != "manager" and user.access_level(Module.COSTING) >= level)


def require_cost(user, level=AccessLevel.VIEW):
    if not cost_access(user, level):
        fail("Нет доступа к внутренней себестоимости", 403)


def can_estimate(user, edit=False):
    level = AccessLevel.EDIT if edit else AccessLevel.VIEW
    if not cost_access(user, level) and user.access_level(Module.CLIENTS) < level:
        fail("Нет доступа к сметам", 403)


def lock(db):
    # Same transaction for source edits, snapshot construction and journal.
    from app.warehouse.documents import advisory
    advisory(db, "costing:write")


def settings_out(db):
    row = db.get(CostingSettings, 1)
    return {"version": row.version if row else 0, "content": SettingsData.model_validate(row.content).model_dump(mode='json') if row else SettingsData().model_dump(mode="json")}


def audit(db, user, entity, id, action, before, after):
    db.add(CostingAudit(entity=entity, entity_id=id, actor_id=user.id, action=action, before=before, after=after))


def shared_price(db, id):
    from sqlalchemy import Numeric, cast, select
    # The legacy warehouse mapper uses asdecimal=False; bypass its float result.
    return db.execute(select(cast(WarehouseMaterial.__table__.c.purchase_price, Numeric(20, 8, asdecimal=True))).where(WarehouseMaterial.id == id)).scalar_one()


def material_out(db, row):
    price = row.price
    name, unit, category = row.name, row.unit, row.category
    warehouse = db.get(WarehouseMaterial, row.warehouse_material_id) if row.warehouse_material_id else None
    archived = row.archived
    if row.warehouse_material_id:
        if not warehouse:
            price = None
        else:
            name, unit, category = warehouse.title, warehouse.unit, warehouse.category.value
            archived = archived or warehouse.is_archived
            # The warehouse remains the price source for linked positions.
            # A legacy zero is unknown until explicitly confirmed in this ledger.
            raw = shared_price(db, warehouse.id)
            price = raw if raw > 0 or (raw == 0 and row.price == 0 and row.price_source) else None
    return {"id": row.id, "version": row.version, "warehouse_material_id": row.warehouse_material_id,
            "name": name, "unit": unit, "category": category, "price": str(price) if price is not None else None,
            "price_source": row.price_source, "effective_on": row.effective_on.isoformat(), "archived": archived}


def item_out(row):
    return {"id": row.id, "kind": row.kind, "title": row.title, "archived": row.archived,
            "version": row.version, "content": row.content}


def profile_for(item, settings):
    data = item["content"]
    profile = next((p for p in settings["profiles"] if p["name"] == data["profile"]), None)
    if profile is None:
        fail("Не найден профиль расходов: " + data["profile"])
    profile = deepcopy(profile)
    profile.setdefault('realtor','0')
    if data.get("margin") is not None:
        profile["margin"] = data["margin"]
    Profile.model_validate(profile)
    return profile


def geometry(data):
    if data.get("modules"):
        return sum((decimal(m["length"]) * decimal(m["width"]) * m["count"] for m in data["modules"]), ZERO)
    if data.get("area") is not None:
        return decimal(data["area"])
    if data.get("length") is not None and data.get("width") is not None:
        return decimal(data["length"]) * decimal(data["width"])
    return None


def materials_cost(lines, materials, today):
    total, missing, detail = ZERO, [], []
    if not lines:
        missing.append("Не заполнен состав изделия")
    for line in lines:
        mat = materials.get(str(line["material_id"]))
        if mat is None:
            fail("Материал не найден: " + str(line["material_id"]))
        volume = decimal(line["quantity"])
        quantity = volume * line.get("pieces", 1)
        manual = line.get("price") is not None
        raw_price = line["price"] if manual else mat["price"]
        price = decimal(raw_price) if raw_price is not None else None
        if price is None or (not manual and mat["effective_on"] > today):
            missing.append("Нет действующей цены: " + mat["name"])
            price = None
        if mat["archived"]:
            missing.append("Материал в архиве: " + mat["name"])
        amount = price * quantity if price is not None else None
        if amount is not None:
            total += amount
        detail.append({**line, "quantity": str(quantity), "volume": str(volume), "pieces": line.get("pieces", 1),
                       "manual_price": manual, "name": mat["name"], "category": mat["category"], "unit": mat["unit"],
                       "price": str(price) if price is not None else None, "amount": money(amount), "material_version": mat["version"]})
    return total, missing, detail


def component(item, settings, materials, today, replacement=True):
    data = item["content"]
    profile = profile_for(item, settings)
    raw, missing, detail = materials_cost(data["lines"], materials, today)
    if data.get('service_only') and not data['lines']: missing=[]
    credit, credit_detail = ZERO, []
    if data.get("replacements") and replacement:
        credit, credit_missing, credit_detail = materials_cost(data["replacements"], materials, today)
        missing += credit_missing
    fixed = ZERO
    for key, label in EXPENSE_LABELS.items():
        value = data["expenses"].get(key)
        if value is None:
            missing.append("Не указан расход: " + label)
        else:
            fixed += decimal(value)
    if not data["expenses"].get("evidence"):
        missing.append("Укажите основание фиксированных расходов, в том числе нулевых")
    base_cost = raw - credit + fixed
    rates = sum((decimal(profile[k]) for k in ("labor", "management", "tax", "realtor")), ZERO) / 100
    denominator = 1 - decimal(profile["margin"]) / 100 - rates
    if denominator <= 0:
        missing.append("Целевая маржа недостижима при заданных процентах")
    if base_cost < 0:
        missing.append("Вычет превышает стоимость допа; требуется отдельный расчёт изменения комплектации")
    calculated = base_cost / denominator if denominator > 0 and not missing else None
    area = geometry(data)
    rate = None
    if calculated is not None:
        if item["kind"] == "template" and data["mode"] == "area":
            if area:
                rate = rounded(calculated / area, settings["rate_step"])
                recommended = rounded(rate * area, settings["total_step"])
            else:
                missing.append("Не указана площадь изделия")
                recommended = None
        else:
            recommended = rounded(calculated, settings["addon_step"] if item["kind"] == "addon" else settings["total_step"])
    else:
        recommended = None
    # Saved retail values never change because materials/settings changed.
    if item["kind"] == "template" and data["mode"] == "area" and data.get("retail_rate") is not None:
        retail = rounded(decimal(data["retail_rate"]) * area, settings["total_step"]) if area else None
    elif data.get("retail_price") is not None:
        retail = decimal(data["retail_price"])
    else:
        retail = None
    return {"item_id": item["id"], "title": item["title"], "kind": item["kind"], "version": item["version"],
            "materials": raw - credit, "fixed": fixed, "base_cost": base_cost, "rates": rates,
            "profile": profile, "recommended": recommended, "recommended_rate": rate, "retail": retail,
            "missing": missing, "lines": detail, "replacement_lines": credit_detail, "area": area}


def sources(db, previous=None):
    old = deepcopy(previous or {})
    settings = old.get("settings") or settings_out(db)["content"]
    items = {str(i.id): item_out(i) for i in db.query(CostingItem).all()}
    materials = {str(m.id): material_out(db, m) for m in db.query(CostingMaterial).all()}
    # Existing materials and item definitions stay frozen on ordinary edits.
    items.update(old.get("items", {}))
    materials.update(old.get("materials", {}))
    from app.costing.governance import populate_item_approvals
    return populate_item_approvals(db, {"settings": settings, "items": items, "materials": materials,
            "priced_on": old.get("priced_on") or datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()})


def inspect_item(item, source):
    if not source.get('production_approved',{}).get(str(item['id']),False):
        from app.costing.technical_basis import item_basis
        return {"price":None,"cost":None,"profit":None,"margin_percent":None,"recommended":None,
                "missing":["До формирования цены состав должен согласовать начальник производства"],
                "lines":[{**line,'price':None,'amount':None} for line in item_basis(item,source)['lines']]}
    if item["kind"] == "package":
        members, missing, listed, base, costs = [], [], ZERO, ZERO, ZERO
        for line in item["content"]["addons"]:
            addon = source["items"].get(str(line["addon_id"]))
            if not addon or addon["kind"] != "addon":
                fail("Пакет содержит неизвестный доп")
            p = component(addon, source["settings"], source["materials"], source["priced_on"])
            fixed_price = item["content"].get("package_prices", {}).get(str(addon["id"]))
            if fixed_price is not None:
                p["retail"] = decimal(fixed_price)
            quantity = decimal(line["quantity"])
            missing += p["missing"]
            if addon["archived"]:
                missing.append("Доп в архиве: " + addon["title"])
            if p["retail"] is None:
                missing.append("Не зафиксирован прайс: " + addon["title"])
            price = (p["retail"] or ZERO) * quantity
            listed += price
            base += p["base_cost"] * quantity
            costs += (price * (1 - decimal(item["content"]["discount"]) / 100)).quantize(D("0.01"), rounding=ROUND_HALF_UP) * p["rates"]
            members.append({"item_id": addon["id"], "title": addon["title"], "quantity": str(quantity), "price": money(p["retail"])})
        if not members:
            missing.append("Пакет пуст")
        price = sum(((decimal(m["price"] or "0") * decimal(m["quantity"]) * (1 - decimal(item["content"]["discount"]) / 100)).quantize(D("0.01"), rounding=ROUND_HALF_UP) for m in members), ZERO)
        cost = base + costs
        return {"price": money(price), "list_price": money(listed), "cost": money(cost) if not missing else None,
                "profit": money(price - cost) if not missing else None,
                "margin_percent": money((price - cost) / price * 100) if price > 0 and not missing else None,
                "below_target": price > 0 and not missing and price - cost < price * D("0.35"),
                "missing": list(dict.fromkeys(missing)), "members": members}
    p = component(item, source["settings"], source["materials"], source["priced_on"])
    price = p["retail"]
    cost = p["base_cost"] + (price or ZERO) * p["rates"]
    return {"materials": money(p["materials"]), "fixed": money(p["fixed"]), "base_cost": money(p["base_cost"]),
            "price": money(price), "recommended": money(p["recommended"]), "recommended_rate": money(p["recommended_rate"]),
            "cost": money(cost) if not p["missing"] and price is not None else None,
            "profit": money(price - cost) if price is not None and not p["missing"] else None,
            "margin_percent": money((price - cost) / price * 100) if price and not p["missing"] else None,
            "below_target": bool(price and not p["missing"] and price - cost < price * max(D(35), decimal(p["profile"]["margin"])) / 100),
            "profile": p["profile"], "area": str(p["area"]) if p["area"] else None,
            "missing": list(dict.fromkeys(p["missing"])), "lines": p["lines"], "replacement_lines": p["replacement_lines"]}


def build_snapshot(db, payload: EstimateInput, previous=None, refresh=False, estimate_id=None):
    from app.costing.governance import build_snapshot as controlled_snapshot
    return controlled_snapshot(db,payload,previous,refresh,estimate_id)


def calculate_snapshot(db, payload: EstimateInput, previous=None, refresh=False):
    source = sources(db, previous.get("sources") if previous and not refresh else None)
    item = deepcopy(source["items"].get(str(payload.template_id)))
    if not item or item["kind"] != "template" or item["archived"]:
        fail("Выберите действующую типовую смету")
    if payload.lines is not None:
        item["content"]["lines"] = [l.model_dump(mode="json") for l in payload.lines]
    if payload.profile:
        source["settings"] = deepcopy(source["settings"])
        override = payload.profile.model_dump(mode="json")
        override["name"] = "Параметры сметы"
        source["settings"]["profiles"] = [p for p in source["settings"]["profiles"] if p["name"] != override["name"]] + [override]
        item["content"]["profile"] = override["name"]
        item["content"]["margin"] = None
    if payload.expenses:
        item["content"]["expenses"] = payload.expenses.model_dump(mode="json")
    base = component(item, source["settings"], source["materials"], source["priced_on"])
    missing = list(base["missing"])
    consumed = {}
    available = {}
    base_materials = {str(line["material_id"]): line for line in base["lines"]}
    for line in base["lines"]:
        key = str(line["material_id"])
        available[key] = available.get(key, ZERO) + decimal(line["quantity"])
    parts = [(base, D(1), D(1), "base")]
    for selection in payload.selections:
        chosen = source["items"].get(str(selection.item_id))
        if not chosen or chosen["archived"] or chosen["kind"] not in ("addon", "package"):
            fail("Выберите действующий доп или пакет")
        count = selection.quantity
        if chosen["kind"] == "package":
            rows = [(source["items"].get(str(l["addon_id"])), count * decimal(l["quantity"]),
                     1 - decimal(chosen["content"]["discount"]) / 100, chosen["title"]) for l in chosen["content"]["addons"]]
            if not rows:
                missing.append("Пакет пуст: " + chosen["title"])
        else:
            rows = [(chosen, count, D(1), "")]
        for addon, quantity, factor, package in rows:
            if not addon or addon["kind"] != "addon" or addon["archived"]:
                fail("Пакет содержит недействующий доп")
            # A replacement refunds the actual base line's price, including a
            # manually priced client line, rather than a different catalog price.
            p = component(addon, source["settings"], source["materials"], source["priced_on"], replacement=False)
            if chosen["kind"] == "package":
                frozen = chosen["content"].get("package_prices", {}).get(str(addon["id"]))
                if frozen is not None:
                    p["retail"] = decimal(frozen)
            for replacement in addon["content"]["replacements"]:
                key = str(replacement["material_id"])
                replace_quantity = decimal(replacement["quantity"]) * replacement.get("pieces", 1)
                consumed[key] = consumed.get(key, ZERO) + replace_quantity * quantity
                if consumed[key] > available.get(key, ZERO):
                    fail("Допы заменяют больше базового материала, чем есть в смете")
                original = base_materials[key]
                credit = replace_quantity * decimal(original["price"]) if original["price"] is not None else ZERO
                p["materials"] -= credit
                p["base_cost"] -= credit
                p["replacement_lines"].append({**original, "quantity": str(replace_quantity), "amount": money(credit)})
            if p["base_cost"] < 0:
                p["missing"].append("Вычет превышает стоимость допа; требуется отдельный расчёт изменения комплектации")
            p["package"] = package
            parts.append((p, quantity, factor, "addon"))
            missing += p["missing"]
    listed, base_cost = ZERO, ZERO
    priced_parts, commercial, bom = [], [], {}
    for p, count, factor, kind in parts:
        price = p["retail"]
        if price is None:
            missing.append("Не зафиксирован прайс: " + p["title"])
            price = ZERO
        amount = (price * count * factor * payload.quantity).quantize(D("0.01"), rounding=ROUND_HALF_UP)
        listed += amount
        base_cost += p["base_cost"] * count * payload.quantity
        priced_parts.append((p, amount))
        commercial.append({"item_id": p["item_id"], "kind": kind, "title": p["title"], "package": p.get("package", ""),
                           "quantity": str(count * payload.quantity), "unit_price": money(price * factor), "amount": money(amount)})
        for sign, lines in ((1, p["lines"]), (-1, p["replacement_lines"])):
            for line in lines:
                key = str(line["material_id"]) + "@" + str(line["price"])
                if key not in bom:
                    bom[key] = {**line, "quantity": ZERO}
                bom[key]["quantity"] += decimal(line["quantity"]) * count * payload.quantity * sign
    if sum((bool(payload.discount), payload.sale_price is not None, payload.discount_amount is not None)) > 1:
        fail("Выберите один способ: скидка в процентах, скидка в рублях или договорная цена")
    price = (payload.sale_price if payload.sale_price is not None else listed - payload.discount_amount
             if payload.discount_amount is not None else listed * (1 - payload.discount / 100)).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    if price < 0:
        fail("Скидка не может превышать сумму по прайсу")
    # Price overrides are final prices. The discount is applied once to list price.
    if payload.sale_price is not None and payload.discount:
        fail("Укажите либо итоговую договорную цену, либо скидку")
    ratio = price / listed if listed > 0 else ZERO
    rate_cost = sum((amount * ratio * p["rates"] for p, amount in priced_parts), ZERO)
    revenue_expenses = [{"label": source["settings"].get(key + "_label",'Комиссия риелтора'),
                         "amount": money(sum((amount * ratio * decimal(p["profile"][key]) / 100 for p, amount in priced_parts), ZERO))}
                        for key in ("labor", "management", "tax", "realtor")]
    cost = base_cost + rate_cost
    profit = price - cost
    margin = profit / price * 100 if price > 0 else None
    weighted_rate = sum((amount * p["rates"] for p, amount in priced_parts), ZERO) / listed if listed > 0 else base["rates"]
    denominator = D("0.65") - weighted_rate
    minimum = (base_cost / denominator).quantize(D("0.01"), rounding=ROUND_CEILING) if denominator > 0 else None
    if not price:
        missing.append("Цена продажи должна быть положительной")
    missing = list(dict.fromkeys(missing))
    result = {"status": "incomplete" if missing else "meets_target" if price > 0 and profit >= price * D("0.35") else "below_target",
              "missing": missing, "price": money(price), "list_price": money(listed), "cost": money(cost) if not missing else None,
              "profit": money(profit) if not missing else None, "margin_percent": money(margin) if not missing else None,
              "minimum_price": money(minimum) if not missing else None, "target_margin_percent": "35",
              "discount_amount": money(listed - price), "base_cost": money(base_cost) if not missing else None,
              "rate_cost": money(rate_cost) if not missing else None, "revenue_expenses": revenue_expenses if not missing else []}
    bom_lines = []
    for line in bom.values():
        if line["quantity"] < 0:
            fail("Количество материала после замены не может быть отрицательным")
        if line["quantity"] == 0:
            continue
        line["amount"] = money(line["quantity"] * decimal(line["price"])) if line["price"] is not None else None
        line["quantity"] = str(line["quantity"])
        bom_lines.append(line)
    # Persist only the sources actually used, avoiding unrelated private data.
    item_ids = {str(payload.template_id)} | {str(s.item_id) for s in payload.selections}
    for key in list(item_ids):
        selected = source["items"].get(key)
        if selected and selected["kind"] == "package":
            item_ids |= {str(l["addon_id"]) for l in selected["content"]["addons"]}
    material_ids = {str(l["material_id"]) for p, _, _, _ in parts for l in p["lines"] + p["replacement_lines"]}
    source["items"] = {k: v for k, v in source["items"].items() if k in item_ids}
    source["materials"] = {k: v for k, v in source["materials"].items() if k in material_ids}
    return {"input": payload.model_dump(mode="json"), "sources": source, "result": result, "commercial": commercial,
            "bom": bom_lines, "profile": base["profile"], "expenses": item["content"]["expenses"],
            "image": payload.image if payload.image is not None else item["content"].get("image", ""),
            "company": source["settings"]["company"], "logo": source["settings"]["logo"]}


def estimate_query(db, user):
    query = db.query(CostingEstimate)
    if not cost_access(user):
        # Ownership never follows a client supplied actor id.
        query = query.filter(CostingEstimate.actor_id == user.id)
    return query


def get_estimate(db, id, user):
    row = estimate_query(db, user).filter(CostingEstimate.id == id).with_for_update().populate_existing().first()
    if not row:
        fail("Смета не найдена", 404)
    return row


def get_revision(db, row, number=None):
    revision = db.query(CostingRevision).filter_by(estimate_id=row.id, number=number or row.version).first()
    if not revision:
        fail("Ревизия сметы не найдена", 404)
    return revision


def estimate_out(db, row, user, number=None):
    revision = get_revision(db, row, number)
    snap = revision.snapshot
    result = {"id": row.id, "title": snap["input"]["title"], "number": snap["input"]["number"], "client_id": snap["input"]["client_id"],
              "version": row.version, "revision": revision.number, "status": row.status, "archived": row.archived,
              "actor_id": row.actor_id, "created_at": row.created_at, "revision_at": revision.created_at,
              "company": snap["company"], "logo": snap["logo"], "image": snap["image"],
              "commercial": snap["commercial"], "price": snap["result"]["price"], "list_price": snap["result"]["list_price"],
              "discount_amount": snap["result"]["discount_amount"], "note": snap["input"]["note"]}
    # Whitelist for commercial users. No recursively redacted financial JSON.
    inp = snap["input"]
    result["input"] = {k: inp.get(k) for k in ("title", "number", "client_id", "template_id", "quantity", "selections", "discount", "discount_amount", "sale_price", "note")}
    result['context']={'client_id':row.client_id,'crm_deal_id':row.crm_deal_id,'sales_order_id':row.sales_order_id,'sales_order_line':row.sales_order_line}
    result['input'].update(crm_deal_id=row.crm_deal_id,sales_order_id=row.sales_order_id,sales_order_line=row.sales_order_line)
    template=snap['sources']['items'][str(inp['template_id'])]['content']
    source_fields=('source_recipe_id','source_recipe_version','warehouse_product_id','house_model_key') if cost_access(user) else ('warehouse_product_id','house_model_key')
    result['source']={k:template.get(k) for k in source_fields}
    result['source']['service_only']=template.get('service_only',False)
    if cost_access(user):
        result.update({"result": snap["result"], "bom": snap["bom"], "profile": snap["profile"], "expenses": snap["expenses"],
                       "priced_on": snap["sources"]["priced_on"],
                       "base_lines": inp["lines"] if inp["lines"] is not None else snap["sources"]["items"][str(inp["template_id"])]["content"]["lines"]})
        result["input"].update({k: inp[k] for k in ("lines", "profile", "expenses", "image")})
        result['input']['overhead_plan_id']=inp.get('overhead_plan_id')
        result['overhead_plan']=snap.get('overhead_plan')
    from app.costing.governance import review_state
    governance=review_state(db,row,revision)
    result['governance']={key:value for key,value in governance.items() if cost_access(user) or key!='issues'}
    if not cost_access(user) and not governance['director_approved']:
        result['price']=result['list_price']=result['discount_amount']=None
        result['commercial']=[{**line,'unit_price':None,'amount':None} for line in result['commercial']]
    return result
