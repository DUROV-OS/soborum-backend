from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.clients.models import Client
from app.common.module_access import AccessLevel, Module
from app.core.deps import get_current_user
from app.costing import service as s
from app.costing.models import CostingAudit, CostingEstimate, CostingItem, CostingMaterial, CostingPriceVersion, CostingRevision, CostingSettings
from app.costing.schemas import ArchiveAction, EstimateInput, ItemInput, MaterialInput, PriceChanges, PriceRestore, PriceVersionInput, SettingsInput, VersionAction
from app.db.session import get_db
from app.users.models import User
from app.warehouse.models import WarehouseMaterial

app = FastAPI(title="Сметы и себестоимость")
from app.costing.catalog_navigation import router as catalog_navigation_router, catalog_brief
app.include_router(catalog_navigation_router)


def view(user: User = Depends(get_current_user)):
    s.can_estimate(user)
    return user


def edit(user: User = Depends(get_current_user)):
    s.can_estimate(user, edit=True)
    return user


def cost_view(user: User = Depends(get_current_user)):
    s.require_cost(user)
    return user


def cost_edit(user: User = Depends(get_current_user)):
    s.require_cost(user, AccessLevel.EDIT)
    return user


def cost_full(user: User = Depends(get_current_user)):
    s.require_cost(user, AccessLevel.FULL)
    return user


def existing(db, model, id):
    row = db.query(model).filter_by(id=id).with_for_update().populate_existing().first()
    if not row:
        s.fail("Запись не найдена", 404)
    return row


def check_version(row, version):
    if version is None or row.version != version:
        s.fail("Запись изменилась. Обновите страницу и повторите правку", 409)


@app.get("/access")
def access(user: User = Depends(view)):
    return {"internal": s.cost_access(user), "edit": s.cost_access(user, AccessLevel.EDIT),
            "full": s.cost_access(user, AccessLevel.FULL), "estimate_edit": s.cost_access(user, AccessLevel.EDIT) or user.access_level(Module.CLIENTS) >= AccessLevel.EDIT}


@app.get("/settings")
def settings(db: Session = Depends(get_db), user: User = Depends(cost_view)):
    return s.settings_out(db)


@app.put("/settings")
def save_settings(body: SettingsInput, db: Session = Depends(get_db), user: User = Depends(cost_full)):
    s.lock(db)
    old = s.settings_out(db)
    if body.version != old["version"]:
        s.fail("Настройки уже изменены", 409)
    names = {p.name for p in body.content.profiles}
    for item in db.query(CostingItem):
        if item.kind != "package" and item.content["profile"] not in names:
            s.fail("Профиль используется изделием: " + item.title)
    row = db.get(CostingSettings, 1)
    if row is None:
        row = CostingSettings(id=1, version=1, content=body.content.model_dump(mode="json"))
        db.add(row)
    else:
        row.version += 1
        row.content = body.content.model_dump(mode="json")
    s.audit(db, user, "settings", 1, "update", old, {"version": row.version, "content": row.content})
    db.commit()
    return s.settings_out(db)


@app.get("/warehouse-catalog")
def warehouse_catalog(query: str = "", limit: int = Query(100, ge=1, le=300), db: Session = Depends(get_db), user: User = Depends(cost_view)):
    rows = db.query(WarehouseMaterial).filter_by(is_archived=False)
    if query.strip():
        rows = rows.filter(WarehouseMaterial.title.contains(query.strip(), autoescape=True))
    return [{"id": m.id, "name": m.title, "unit": m.unit, "category": m.category.value,
             "warehouse": m.warehouse.value, "price": str(m.purchase_price) if m.purchase_price else None} for m in rows.order_by(WarehouseMaterial.title).limit(limit)]


@app.get("/materials")
def materials(query: str = "", archive: str = Query("active", pattern="^(active|archived|all)$"), category: str = "",
              unused: bool = False, db: Session = Depends(get_db), user: User = Depends(cost_view)):
    used = {l["material_id"] for i in db.query(CostingItem).all() for l in i.content.get("lines", []) + i.content.get("replacements", [])}
    rows = [s.material_out(db, m) for m in db.query(CostingMaterial).order_by(CostingMaterial.name)]
    return [m for m in rows if (archive == "all" or m["archived"] == (archive == "archived"))
            and (not category or m["category"] == category) and (not unused or m["id"] not in used)
            and (not query or query.casefold() in (m["name"] + " " + m["category"]).casefold())]


def write_material(db, user, body, id=None):
    s.lock(db)
    row = existing(db, CostingMaterial, id) if id else None
    old = s.material_out(db, row) if row else None
    if row:
        check_version(row, body.version)
        if body.warehouse_material_id != row.warehouse_material_id:
            s.fail("Для смены единицы или источника создайте новую позицию; архивные сметы должны оставаться читаемыми")
        if body.unit != old["unit"]:
            s.fail("Единицу используемой позиции не менять. Создайте новую позицию")
    if body.price is not None and not body.price_source:
        s.fail("Укажите источник цены, в том числе нулевой")
    if row and body.archived != row.archived:
        s.require_cost(user, AccessLevel.FULL)
    values = body.model_dump(exclude={"version"})
    if body.warehouse_material_id:
        warehouse = existing(db, WarehouseMaterial, body.warehouse_material_id)
        if warehouse.is_archived:
            s.fail("Складская позиция в архиве")
        match = db.query(CostingMaterial).filter_by(warehouse_material_id=warehouse.id).first()
        if match and (row is None or match.id != row.id):
            s.fail("Складская позиция уже связана со справочником", 409)
        if body.unit != warehouse.unit:
            s.fail("Единица должна совпадать с единицей склада")
        values.update(name=warehouse.title, category=warehouse.category.value, unit=warehouse.unit)
        actual = s.shared_price(db, warehouse.id)
        if body.price is not None and body.price != actual:
            if user.access_level(Module.WAREHOUSE) < AccessLevel.EDIT:
                s.fail("Изменение общей складской цены требует права редактирования склада", 403)
            warehouse.purchase_price = body.price
        # Empty input on a link uses the shared warehouse price; no shadow override.
        if body.price is None:
            values["price"] = actual if actual > 0 else None
            values["price_source"] = body.price_source or ("Складская закупочная цена" if actual > 0 else "")
    if row:
        for key, value in values.items():
            setattr(row, key, value)
        row.version += 1
    else:
        row = CostingMaterial(**values, version=1)
        db.add(row)
    db.flush()
    after = s.material_out(db, row)
    s.audit(db, user, "material", row.id, "update" if old else "create", old, after)
    db.commit()
    return after


@app.post("/materials", status_code=201)
def add_material(body: MaterialInput, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    return write_material(db, user, body)


@app.put("/materials/{id}")
def update_material(id: int, body: MaterialInput, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    return write_material(db, user, body, id)


@app.post("/materials/import")
async def import_materials(file: UploadFile, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    from app.costing.exports import read_materials
    raw = await file.read(5 * 1024 * 1024 + 1)
    rows = read_materials(raw)
    from pydantic import ValidationError
    try:
        validated = [MaterialInput.model_validate(payload) for payload in rows]
    except ValidationError:
        s.fail("Проверьте наименования, единицы, числа и даты всех строк файла")
    if any(body.price is not None and not body.price_source for body in validated):
        s.fail("Укажите источник каждой цены")
    s.lock(db)
    created = []
    # Explicit import creates planning materials only. It never overwrites shared stock prices.
    for body in validated:
        row = CostingMaterial(**body.model_dump(exclude={"version"}), version=1)
        db.add(row)
        db.flush()
        s.audit(db, user, "material", row.id, "import", None, s.material_out(db, row))
        created.append(row.id)
    db.commit()
    return {"created_ids": created}


@app.get("/items")
def items(kind: str = Query("template", pattern="^(template|addon|package)$"), query: str = "",
          archive: str = Query("active", pattern="^(active|archived|all)$"), db: Session = Depends(get_db), user: User = Depends(cost_view)):
    source = s.sources(db)
    rows = [i for i in source["items"].values() if i["kind"] == kind and (archive == "all" or i["archived"] == (archive == "archived"))
            and (not query or query.casefold() in (i["title"] + " " + i["content"]["code"] + " " + i["content"]["category"] + " " + i["content"]["model"]).casefold())]
    return [{**row, "calculation": s.inspect_item(row, source)} for row in sorted(rows, key=lambda i: i["title"])]


def validate_item(db, body):
    data = body.content
    from app.costing.integration import validate_item_sources
    validate_item_sources(db,data)
    settings = s.settings_out(db)["content"]
    if body.kind != "package" and data.profile not in {p["name"] for p in settings["profiles"]}:
        s.fail("Выберите действующий профиль расходов")
    if body.kind != "package":
        try:
            s.profile_for({"content": data.model_dump(mode="json")}, settings)
        except ValueError:
            s.fail("Сумма целевой маржи и расходов от выручки должна быть меньше 100%")
    if body.kind != "addon" and data.replacements:
        s.fail("Замена материала задаётся только для допа")
    if body.kind == "package" and (data.lines or data.replacements):
        s.fail("Состав пакета задаётся списком допов")
    if body.kind != "package" and data.addons:
        s.fail("Список допов задаётся только для пакета")
    if data.package_prices and (body.kind != "package" or not set(data.package_prices).issubset({str(l.addon_id) for l in data.addons})):
        s.fail("Замороженные цены должны относиться к допам этого пакета")
    for line in data.lines + data.replacements:
        mat = db.get(CostingMaterial, line.material_id)
        if not mat or s.material_out(db, mat)["archived"]:
            s.fail("Выберите действующий материал")
    for line in data.addons:
        addon = db.get(CostingItem, line.addon_id)
        if not addon or addon.kind != "addon" or addon.archived:
            s.fail("Выберите действующий доп")


def write_item(db, user, body, id=None):
    s.lock(db)
    validate_item(db, body)
    row = existing(db, CostingItem, id) if id else None
    before = s.item_out(row) if row else None
    if row:
        check_version(row, body.version)
        if row.kind != body.kind:
            s.fail("Тип записи нельзя изменить")
        if body.archived != row.archived:
            s.require_cost(user, AccessLevel.FULL)
        row.version += 1
    else:
        row = CostingItem(kind=body.kind, version=1)
        db.add(row)
    row.title, row.archived, row.content = body.title, body.archived, body.content.model_dump(mode="json")
    db.flush()
    after = s.item_out(row)
    s.audit(db, user, body.kind, row.id, "update" if before else "create", before, after)
    db.commit()
    return {**after, "calculation": s.inspect_item(after, s.sources(db))}


@app.post("/items", status_code=201)
def add_item(body: ItemInput, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    return write_item(db, user, body)


@app.put("/items/{id}")
def update_item(id: int, body: ItemInput, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    return write_item(db, user, body, id)


@app.post("/items/{id}/freeze-package")
def freeze_package(id: int, body: VersionAction, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    s.lock(db)
    row = existing(db, CostingItem, id)
    check_version(row, body.version)
    if row.kind != "package" or row.archived:
        s.fail("Выберите действующий пакет")
    source = s.sources(db)
    prices = {}
    for line in row.content["addons"]:
        addon = source["items"].get(str(line["addon_id"]))
        if not addon or addon["archived"]:
            s.fail("Пакет содержит недействующий доп")
        price = s.inspect_item(addon, source)["price"]
        if price is None:
            s.fail("Зафиксируйте розничные цены всех допов пакета")
        prices[str(addon["id"])] = price
    if not prices:
        s.fail("Пакет пуст")
    before = s.item_out(row)
    row.content = {**deepcopy(row.content), "package_prices": prices}
    row.version += 1
    s.audit(db, user, "package", id, "freeze", before, {**s.item_out(row), "reason": body.reason})
    db.commit()
    return s.item_out(row)


@app.get("/catalog")
def catalog(db: Session = Depends(get_db), user: User = Depends(view)):
    source = s.sources(db)
    result = []
    for item in source["items"].values():
        if item["archived"]:
            continue
        result.append(catalog_brief(item,source))
    return result


@app.get("/items/{id}/export/{format}")
def export_item(id: int, format: str, no_prices: bool = False, db: Session = Depends(get_db), user: User = Depends(cost_view)):
    from app.costing.exports import estimate_document
    item = s.item_out(existing(db, CostingItem, id))
    if item["kind"] == "package":
        s.fail("Для пакета используйте прайс или клиентскую смету")
    source = s.sources(db)
    calculation = s.inspect_item(item, source)
    data = {"id": id, "revision": item["version"], "number": item["content"]["code"], "title": item["title"],
            "company": source["settings"]["company"], "logo": source["settings"]["logo"], "note": item["content"]["note"],
            "bom": calculation["lines"], "commercial": [], "price": calculation["price"], "list_price": calculation["price"],
            "discount_amount": "0.00", "result": calculation}
    raw, content_type = estimate_document(data, format, True, no_prices)
    return Response(raw, media_type=content_type, headers={"Content-Disposition": f'attachment; filename="item-{id}-v{item["version"]}.{format}"'})


@app.post("/prices")
def save_prices(body: PriceChanges, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    s.lock(db)
    if len({p.id for p in body.items}) != len(body.items):
        s.fail("Повторяющиеся строки прайса")
    for change in sorted(body.items, key=lambda p: p.id):
        row = existing(db, CostingItem, change.id)
        check_version(row, change.version)
        if row.kind == "package":
            s.fail("Цена пакета определяется его составом и скидкой")
        before = s.item_out(row)
        content = deepcopy(row.content)
        if content["mode"] == "area" and row.kind == "template":
            if change.rate is None or change.price is not None or not s.geometry(content):
                s.fail("Укажите ставку за м² и площадь изделия")
            content["retail_rate"], content["retail_price"] = str(change.rate), None
        else:
            if change.price is None or change.rate is not None:
                s.fail("Укажите цену за изделие")
            content["retail_price"], content["retail_rate"] = str(change.price), None
        row.content, row.version = content, row.version + 1
        s.audit(db, user, "price", row.id, "freeze", before, {**s.item_out(row), "reason": body.reason})
    db.commit()
    return {"updated": len(body.items)}


@app.get("/price-versions")
def price_versions(db: Session = Depends(get_db), user: User = Depends(cost_view)):
    return [{"id": r.id, "title": r.title, "actor_id": r.actor_id, "created_at": r.created_at}
            for r in db.query(CostingPriceVersion).order_by(CostingPriceVersion.id.desc()).limit(200)]


@app.post("/price-versions", status_code=201)
def freeze_price_version(body: PriceVersionInput, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    s.lock(db)
    row = CostingPriceVersion(title=body.title, actor_id=user.id, snapshot=[s.item_out(i) for i in db.query(CostingItem).order_by(CostingItem.id)])
    db.add(row)
    db.flush()
    s.audit(db, user, "price_version", row.id, "create", None, {"title": row.title})
    db.commit()
    return {"id": row.id, "title": row.title}


@app.get("/price-versions/{id}")
def price_version(id: int, db: Session = Depends(get_db), user: User = Depends(cost_view)):
    row = existing(db, CostingPriceVersion, id)
    return {"id": row.id, "title": row.title, "created_at": row.created_at, "items": row.snapshot}


@app.post("/price-versions/{id}/restore")
def restore_prices(id: int, body: PriceRestore, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    s.lock(db)
    version = existing(db, CostingPriceVersion, id)
    rows = []
    if set(body.versions) != {item["id"] for item in version.snapshot}:
        s.fail("Обновите состав версии прайса перед восстановлением", 409)
    for item in version.snapshot:
        row = existing(db, CostingItem, item["id"])
        check_version(row, body.versions[row.id])
        if row.kind != item["kind"] or row.content["mode"] != item["content"]["mode"]:
            s.fail("Базис цены изменился: " + row.title)
        if row.kind == "package" and not set(item["content"].get("package_prices", {})).issubset({str(l["addon_id"]) for l in row.content["addons"]}):
            s.fail("Состав пакета изменился: " + row.title)
        rows.append((row, item))
    for row, item in rows:
        before = s.item_out(row)
        row.content = {**deepcopy(row.content), **{key: item["content"].get(key, {} if key == "package_prices" else None) for key in ("retail_price", "retail_rate", "package_prices")}}
        row.version += 1
        s.audit(db, user, "price", row.id, "restore", before, {**s.item_out(row), "price_version_id": id, "reason": body.reason})
    db.commit()
    return {"updated": len(rows)}


@app.get("/journal")
def journal(entity: str = "", entity_id: int | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
            db: Session = Depends(get_db), user: User = Depends(cost_view)):
    # Estimate audits may contain client metadata; use the estimate-scoped history instead.
    query = db.query(CostingAudit).filter(CostingAudit.entity != "estimate")
    if entity:
        query = query.filter_by(entity=entity)
    if entity_id is not None:
        query = query.filter_by(entity_id=entity_id)
    return {"total": query.count(), "items": [{"id": r.id, "entity": r.entity, "entity_id": r.entity_id, "action": r.action,
            "actor_id": r.actor_id, "created_at": r.created_at, "before": r.before, "after": r.after}
            for r in query.order_by(CostingAudit.id.desc()).offset(offset).limit(limit)]}


@app.get("/assistant")
def assistant(db: Session = Depends(get_db), user: User = Depends(cost_view)):
    source = s.sources(db)
    used = {str(l["material_id"]) for item in source["items"].values() if not item["archived"]
            for l in item["content"]["lines"] + item["content"]["replacements"]}
    signals = []
    for mat in source["materials"].values():
        if mat["archived"]:
            continue
        if str(mat["id"]) not in used:
            signals.append({"kind": "unused", "title": mat["name"], "entity": "material", "id": mat["id"], "message": "Не используется в изделиях"})
        if mat["price"] is None or mat["effective_on"] > source["priced_on"]:
            signals.append({"kind": "missing_price", "title": mat["name"], "entity": "material", "id": mat["id"], "message": "Нет действующей закупочной цены"})
    for item in source["items"].values():
        if item["archived"]:
            continue
        calc = s.inspect_item(item, source)
        if calc["missing"]:
            signals.append({"kind": "incomplete", "title": item["title"], "entity": item["kind"], "id": item["id"], "message": "; ".join(calc["missing"])})
        if calc.get("below_target"):
            signals.append({"kind": "below_target", "title": item["title"], "entity": item["kind"], "id": item["id"], "message": "Маржа по прайсу ниже цели или минимума 35%"})
        if calc.get("recommended") is not None and calc.get("price") is not None and calc["price"] != calc["recommended"]:
            signals.append({"kind": "price_difference", "title": item["title"], "entity": item["kind"], "id": item["id"],
                            "message": "Прайс " + calc["price"] + "; расчёт " + calc["recommended"]})
    return signals


def check_client(db, payload, user):
    from app.costing.integration import validate_context
    validate_context(db,payload,user)
    if payload.client_id:
        client = db.get(Client, payload.client_id)
        if not client or (not s.cost_access(user) and client.manager_id != user.id):
            s.fail("Клиент недоступен для создания сметы", 403)


def check_internal_input(payload, user):
    if not s.cost_access(user) and any(v is not None for v in (payload.lines, payload.profile, payload.expenses, payload.image)):
        s.fail("Нет права изменять внутренний расчёт", 403)


@app.post("/estimates/preview")
def preview(body: EstimateInput, estimate_id: int | None = Query(None, gt=0), db: Session = Depends(get_db), user: User = Depends(edit)):
    check_internal_input(body, user)
    check_client(db, body, user)
    previous = None
    if estimate_id:
        row = s.get_estimate(db, estimate_id, user)
        check_version(row, body.version)
        previous = s.get_revision(db, row).snapshot
    snapshot = s.build_snapshot(db, body, previous)
    if s.cost_access(user):
        return {"result": snapshot["result"], "commercial": snapshot["commercial"], "bom": snapshot["bom"]}
    return {"price": snapshot["result"]["price"], "commercial": snapshot["commercial"],
            "ready": snapshot["result"]["status"] == "meets_target"}


@app.get("/clients-catalog")
def clients_catalog(query: str = "", db: Session = Depends(get_db), user: User = Depends(view)):
    rows = db.query(Client)
    if not s.cost_access(user):
        rows = rows.filter(Client.manager_id == user.id)
    if query:
        rows = rows.filter(Client.full_name.contains(query, autoescape=True))
    return [{"id": r.id, "title": r.full_name} for r in rows.order_by(Client.id.desc()).limit(200)]


def save_estimate(db, user, body, id=None, refresh=False):
    s.lock(db)
    check_internal_input(body, user)
    check_client(db, body, user)
    # Replay is scoped to a visible estimate; altered request data is rejected.
    prior = db.query(CostingRevision).filter_by(request_id=str(body.request_id)).first()
    request_data = {"estimate_id": id, "refresh": refresh, "body": body.model_dump(mode="json")}
    if prior:
        row = s.get_estimate(db, prior.estimate_id, user)
        if prior.request_data != request_data:
            s.fail("Идентификатор запроса уже использован с другими данными", 409)
        return s.estimate_out(db, row, user, prior.number)
    row = s.get_estimate(db, id, user) if id else None
    if row:
        check_version(row, body.version)
        if row.archived:
            s.fail("Смета в архиве")
        previous = s.get_revision(db, row).snapshot
    else:
        previous = None
    snapshot = s.build_snapshot(db, body, previous, refresh)
    if row:
        row.version += 1
    else:
        row = CostingEstimate(actor_id=user.id, version=1, status="draft", archived=False, title=body.title)
        db.add(row)
    row.title, row.number, row.client_id, row.status = body.title, body.number, body.client_id, "draft"
    row.crm_deal_id, row.sales_order_id, row.sales_order_line = body.crm_deal_id, body.sales_order_id, body.sales_order_line
    db.flush()
    revision = CostingRevision(estimate_id=row.id, number=row.version, actor_id=user.id, reason=body.reason,
                              request_id=str(body.request_id), request_data=request_data, snapshot=snapshot)
    db.add(revision)
    s.audit(db, user, "estimate", row.id, "reprice" if refresh else "revision" if previous else "create",
            {"revision": row.version - 1} if previous else None, {"revision": row.version, "reason": body.reason})
    db.commit()
    return s.estimate_out(db, row, user)


@app.post("/estimates", status_code=201)
def add_estimate(body: EstimateInput, db: Session = Depends(get_db), user: User = Depends(edit)):
    return save_estimate(db, user, body)


@app.put("/estimates/{id}")
def update_estimate(id: int, body: EstimateInput, db: Session = Depends(get_db), user: User = Depends(edit)):
    return save_estimate(db, user, body, id)


@app.post("/estimates/{id}/reprice")
def reprice(id: int, body: EstimateInput, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    return save_estimate(db, user, body, id, refresh=True)


@app.post("/estimates/{id}/reprice-preview")
def reprice_preview(id: int, body: EstimateInput, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    row = s.get_estimate(db, id, user)
    check_version(row, body.version)
    previous = s.get_revision(db, row).snapshot
    updated = s.build_snapshot(db, body, previous, refresh=True)
    return {"before": previous["result"], "after": updated["result"], "before_bom": previous["bom"], "after_bom": updated["bom"]}


@app.post("/estimates/{id}/copy", status_code=201)
def copy_estimate(id: int, body: VersionAction, db: Session = Depends(get_db), user: User = Depends(edit)):
    s.lock(db)
    original = s.get_estimate(db, id, user)
    check_version(original, body.version)
    request_data = {"copy_from": id, "body": body.model_dump(mode="json")}
    replay = db.query(CostingRevision).filter_by(request_id=str(body.request_id)).first()
    if replay:
        if replay.request_data != request_data:
            s.fail("Идентификатор запроса уже использован", 409)
        return s.estimate_out(db, s.get_estimate(db, replay.estimate_id, user), user)
    snapshot = deepcopy(s.get_revision(db, original).snapshot)
    title = (original.title[:240] + " — копия")
    snapshot["input"].update(title=title, number="", version=None, request_id=str(body.request_id), reason=body.reason,sales_order_id=None,sales_order_line=None)
    row = CostingEstimate(title=title, number="", client_id=original.client_id,crm_deal_id=original.crm_deal_id, actor_id=user.id, version=1, status="draft", archived=False)
    db.add(row)
    db.flush()
    db.add(CostingRevision(estimate_id=row.id, number=1, actor_id=user.id, reason=body.reason, request_id=str(body.request_id),
                          request_data=request_data, snapshot=snapshot))
    s.audit(db, user, "estimate", row.id, "copy", None, {"source_id": id, "source_revision": original.version})
    db.commit()
    return s.estimate_out(db, row, user)


@app.get("/estimates")
def estimates(query: str = "", archive: str = Query("active", pattern="^(active|archived|all)$"),
              date_from: str = "", date_to: str = "", client_id: int | None = None,
              crm_deal_id: int | None = None, sales_order_id: int | None = None,
              offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db), user: User = Depends(view)):
    q = s.estimate_query(db, user)
    if archive != "all":
        q = q.filter(CostingEstimate.archived.is_(archive == "archived"))
    if client_id is not None:
        q = q.filter(CostingEstimate.client_id == client_id)
    if crm_deal_id is not None:
        q = q.filter(CostingEstimate.crm_deal_id == crm_deal_id)
    if sales_order_id is not None:
        q = q.filter(CostingEstimate.sales_order_id == sales_order_id)
    if query:
        from sqlalchemy import or_
        q = q.filter(or_(CostingEstimate.title.contains(query, autoescape=True), CostingEstimate.number.contains(query, autoescape=True)))
    if date_from:
        from datetime import date
        try:
            start = date.fromisoformat(date_from)
        except ValueError:
            s.fail("Неверная дата начала")
        q = q.filter(CostingEstimate.created_at >= datetime.combine(start, datetime.min.time(), ZoneInfo("Europe/Moscow")))
    if date_to:
        from datetime import date, timedelta
        try:
            end = date.fromisoformat(date_to) + timedelta(days=1)
        except ValueError:
            s.fail("Неверная дата окончания")
        q = q.filter(CostingEstimate.created_at < datetime.combine(end, datetime.min.time(), ZoneInfo("Europe/Moscow")))
    rows = q.order_by(CostingEstimate.id.desc()).offset(offset).limit(limit).all()
    return {"total": q.count(), "items": [s.estimate_out(db, r, user) for r in rows]}


@app.get("/estimates/{id}")
def estimate(id: int, revision: int | None = Query(None, ge=1), db: Session = Depends(get_db), user: User = Depends(view)):
    return s.estimate_out(db, s.get_estimate(db, id, user), user, revision)


@app.get("/estimates/{id}/history")
def history(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    row = s.get_estimate(db, id, user)
    result = []
    for rev in db.query(CostingRevision).filter_by(estimate_id=row.id).order_by(CostingRevision.number.desc()):
        data = {"revision": rev.number, "created_at": rev.created_at, "actor_id": rev.actor_id,
                "price": rev.snapshot["result"]["price"]}
        if s.cost_access(user):
            data.update(reason=rev.reason, cost=rev.snapshot["result"]["cost"], margin_percent=rev.snapshot["result"]["margin_percent"])
        result.append(data)
    return result


@app.post("/estimates/{id}/approve")
def approve(id: int, body: VersionAction, db: Session = Depends(get_db), user: User = Depends(cost_edit)):
    s.lock(db)
    row = s.get_estimate(db, id, user)
    check_version(row, body.version)
    if row.archived:
        s.fail("Смета в архиве")
    if row.status == "approved":
        return s.estimate_out(db, row, user)
    rev = s.get_revision(db, row)
    if rev.snapshot["result"]["status"] != "meets_target":
        s.fail("Для утверждения нужны полный расчёт и маржа не ниже 35%")
    # Approval is for this frozen estimate revision, not a stock/CRM posting.
    row.status = "approved"
    s.audit(db, user, "estimate", id, "approve", None, {"revision": row.version, "reason": body.reason})
    db.commit()
    return s.estimate_out(db, row, user)


@app.post("/estimates/{id}/archive")
def archive_estimate(id: int, body: ArchiveAction, db: Session = Depends(get_db), user: User = Depends(cost_full)):
    s.lock(db)
    row = s.get_estimate(db, id, user)
    check_version(row, body.version)
    if row.archived == body.archived:
        return s.estimate_out(db, row, user)
    row.archived = body.archived
    s.audit(db, user, "estimate", id, "archive", None, {"revision": row.version, "archived": row.archived, "reason": body.reason})
    db.commit()
    return s.estimate_out(db, row, user)


@app.get("/exports/{kind}")
def export_catalog(kind: str, db: Session = Depends(get_db), user: User = Depends(view)):
    from app.costing.exports import workbook
    if kind == "materials":
        s.require_cost(user)
        headers = ["Наименование", "Категория", "Единица", "Цена", "Действует с", "Источник"]
        rows = [[m["name"], m["category"], m["unit"], m["price"], m["effective_on"], m["price_source"]]
                for m in [s.material_out(db, r) for r in db.query(CostingMaterial).filter_by(archived=False)]]
    elif kind in ("template", "addon", "package"):
        headers = ["Изделие", "Артикул", "Категория", "Единица", "Площадь", "Цена"]
        rows = [[i["title"], i["code"], i["category"], i["unit"], i["area"], i["price"]] for i in catalog(db, user) if i["kind"] == kind]
    else:
        s.fail("Неизвестная выгрузка", 404)
    return Response(workbook(headers, rows, "Справочник" if kind == "materials" else "Прайс"), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="costing-' + kind + '.xlsx"'})


@app.get("/estimates/{id}/export/{format}")
def export_estimate(id: int, format: str, revision: int | None = Query(None, ge=1), internal: bool = False, no_prices: bool = False,
                    db: Session = Depends(get_db), user: User = Depends(view)):
    from app.costing.exports import estimate_document
    if internal:
        s.require_cost(user)
    row = s.get_estimate(db, id, user)
    data = s.estimate_out(db, row, user, revision)
    raw, content_type = estimate_document(data, format, internal, no_prices)
    return Response(raw, media_type=content_type, headers={"Content-Disposition": f'attachment; filename="estimate-{id}-v{data["revision"]}.{format}"'})


from app.costing.integration import router as integration_router
app.include_router(integration_router)
