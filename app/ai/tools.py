"""Registry of tools the AI assistant can call, grouped by business domain.

Every handler is `handler(db, user, **tool_input) -> dict` and does real work
by calling straight into the same section services the regular REST API
uses (app.clients.service, app.production.service, ...) - no parallel
business logic. A handler may raise HTTPException; the engine turns that
into an `is_error` tool_result so the model can react instead of the whole
turn blowing up.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai.models import ChatDomain
from app.clients import service as client_service
from app.clients.models import Client, ClientStage, OrderType, parse_payment_plan
from app.clients.schemas import (
    ClientBalancePaymentUpdate,
    ClientDocumentsUpdate,
    ClientPaymentUpdate,
    ClientProjectUpdate,
)
from app.common.file_text import extract_asset_text, file_card
from app.common.files import FileAsset, FilePurpose, save_text_file
from app.common.module_access import Module
from app.cycle.models import Cycle
from app.marketing import service as marketing_service
from app.marketing.models import ContentItem
from app.marketing.schemas import ContentAnalysisUpdate, ContentFinalUpdate, ContentItemCreate, ContentRawUpdate
from app.production import service as production_service
from app.production.models import MaterialRequest, MaterialRequestStatus, ProductionModule
from app.production.schemas import ModuleCreate, ModuleMaterialCreate
from app.tasks import service as task_service
from app.tasks.models import Task, TaskStatus
from app.users import service as user_service
from app.users.models import User
from app.warehouse import service as warehouse_service
from app.warehouse.schemas import SupplyCreate, SupplyLineCreate, WarehouseMaterialUpdate


@dataclass
class ToolDef:
    schema: dict
    handler: Callable[..., dict]
    required_module: Module
    read_only: bool = False


TOOLS: dict[str, ToolDef] = {}
DOMAIN_TOOLS: dict[ChatDomain, list[str]] = {d: [] for d in ChatDomain}


def register(name: str, description: str, properties: dict, required: list[str] | None = None, *,
             required_module: Module, read_only: bool = False, domains: list[ChatDomain]):
    def decorator(fn: Callable[..., dict]):
        TOOLS[name] = ToolDef(
            schema={
                "name": name,
                "description": description,
                "input_schema": {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False},
            },
            handler=fn,
            required_module=required_module,
            read_only=read_only,
        )
        for d in domains:
            DOMAIN_TOOLS[d].append(name)
        if ChatDomain.GENERAL not in domains:
            DOMAIN_TOOLS[ChatDomain.GENERAL].append(name)
        return fn

    return decorator


def describe_tool_call(tool_name: str, tool_input: dict) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
    return f"{tool_name}({args})"


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


# ---------------------------------------------------------------- clients --

def _attached_specs(client: Client) -> dict:
    """Project/contract PDFs are the source of truth — not wishes/layout junk fields."""
    project = extract_asset_text(client.house_project_file) if client.house_project_file else None
    contract = extract_asset_text(client.contract_file) if client.contract_file else None
    model = (project or {}).get("model_hint") or (contract or {}).get("model_hint")
    return {
        "house_project_file": file_card(client.house_project_file),
        "contract_file": file_card(client.contract_file),
        "project_spec": project,
        "contract_spec": contract,
        "project_model": model,
        "must_use_attached_files": bool(client.house_project_file_id or client.contract_file_id),
        "field_warning": (
            "Поля wishes_description / layout_notes / house_area часто тестовый мусор. "
            "Если project_spec или contract_spec есть — строй модули и задачи по ним. "
            "Не говори, что спецификации нет."
            if (client.house_project_file_id or client.contract_file_id)
            else "Прикреплённых файлов проекта нет."
        ),
    }


def _serialize_client(c: Client) -> dict:
    return {
        "id": c.id,
        "cycle_id": c.cycle_id,
        "stage": c.stage.value,
        "full_name": c.full_name,
        "phone": c.phone,
        "email": c.email,
        "contacts": c.contacts,
        "order_type": c.order_type.value if c.order_type else None,
        "houses_count": c.houses_count,
        "wishes_description": c.wishes_description,
        "estimated_price": c.estimated_price,
        "house_area": c.house_area,
        "layout_notes": c.layout_notes,
        "project_locked": c.project_locked_at is not None,
        "final_price": c.final_price,
        "payment_plan": c.payment_plan.value,
        "advance_amount": c.advance_amount,
        "installation_address": c.installation_address,
        "contract_file_id": c.contract_file_id,
        "house_project_file_id": c.house_project_file_id,
        **_attached_specs(c),
        "documents_locked": c.documents_locked_at is not None,
        "is_paid": c.is_paid,
        "payment_locked": c.payment_locked_at is not None,
        "balance_paid": c.balance_paid,
        "notes": [{"id": n.id, "text": n.text, "created_at": _iso(n.created_at)} for n in c.notes],
    }


@register(
    "read_attached_file",
    "Прочитать прикреплённый файл клиента (проект дома или договор) и вернуть текст. "
    "ОБЯЗАТЕЛЬНО вызови, если у клиента есть house_project_file или contract_file, "
    "прежде чем говорить, что спецификации нет, или создавать модули и задачи.",
    {"file_id": {"type": "integer"}},
    ["file_id"],
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.CLIENTS, ChatDomain.PRODUCTION, ChatDomain.CYCLE],
)
def _read_attached_file(db: Session, user: User, file_id: int) -> dict:
    asset = db.get(FileAsset, file_id)
    if asset is None:
        return {"error": "Файл не найден"}
    return extract_asset_text(asset)


@register(
    "get_client", "Получить полную информацию о клиенте по его id.",
    {"client_id": {"type": "integer"}}, ["client_id"],
    required_module=Module.CLIENTS, read_only=True, domains=[ChatDomain.CLIENTS],
)
def _get_client(db: Session, user: User, client_id: int) -> dict:
    return _serialize_client(client_service.get_client_or_404(db, client_id))


@register(
    "list_clients", "Список клиентов, опционально отфильтрованный по стадии.",
    {"stage": {"type": "string", "enum": [s.value for s in ClientStage]}},
    required_module=Module.CLIENTS, read_only=True, domains=[ChatDomain.CLIENTS],
)
def _list_clients(db: Session, user: User, stage: str | None = None) -> dict:
    query = db.query(Client)
    if stage:
        query = query.filter(Client.stage == ClientStage(stage))
    clients = query.order_by(Client.id.desc()).limit(50).all()
    return {"clients": [{"id": c.id, "full_name": c.full_name, "stage": c.stage.value} for c in clients]}


@register(
    "add_client_note", "Добавить заметку по клиенту.",
    {"client_id": {"type": "integer"}, "text": {"type": "string"}}, ["client_id", "text"],
    required_module=Module.CLIENTS, domains=[ChatDomain.CLIENTS],
)
def _add_client_note(db: Session, user: User, client_id: int, text: str) -> dict:
    client = client_service.get_client_or_404(db, client_id)
    note = client_service.add_note(db, client, user.id, text)
    return {"note_id": note.id, "text": note.text}


@register(
    "attach_generated_document",
    "Сгенерировать и приложить клиенту документ (проект дома или договор). "
    "content - это ПОЛНЫЙ готовый текст документа, который ты сам пишешь.",
    {
        "client_id": {"type": "integer"},
        "document_type": {"type": "string", "enum": ["house_project", "contract"]},
        "filename": {"type": "string"},
        "content": {"type": "string"},
    },
    ["client_id", "document_type", "filename", "content"],
    required_module=Module.CLIENTS, domains=[ChatDomain.CLIENTS],
)
def _attach_generated_document(
    db: Session, user: User, client_id: int, document_type: str, filename: str, content: str
) -> dict:
    client = client_service.get_client_or_404(db, client_id)
    purpose = FilePurpose.HOUSE_PROJECT if document_type == "house_project" else FilePurpose.CONTRACT
    asset = save_text_file(db, filename, content, purpose, user)
    if document_type == "house_project":
        client_service.set_house_project_file(db, client, asset.id)
    else:
        client_service.set_contract_file(db, client, asset.id)
    return {"file_id": asset.id, "filename": asset.filename, "document_type": document_type}


@register(
    "update_client_project", "Заполнить/изменить проектные данные клиента (пока не зафиксированы).",
    {
        "client_id": {"type": "integer"},
        "order_type": {"type": "string", "enum": [t.value for t in OrderType]},
        "wishes_description": {"type": "string"},
        "estimated_price": {"type": "number"},
        "house_area": {"type": "number"},
        "layout_notes": {"type": "string"},
    },
    ["client_id"],
    required_module=Module.CLIENTS, domains=[ChatDomain.CLIENTS],
)
def _update_client_project(db: Session, user: User, client_id: int, **fields) -> dict:
    client = client_service.get_client_or_404(db, client_id)
    payload = ClientProjectUpdate(**{k: v for k, v in fields.items() if v is not None})
    client = client_service.update_project(db, client, payload)
    return _serialize_client(client)


@register(
    "update_client_documents", "Заполнить/изменить документные данные клиента (пока не зафиксированы). "
    "payment_plan принимает значение enum ('full_prepayment', 'advance_then_balance', 'post_payment') "
    "или русский лейбл («Полная предоплата», «Аванс + оплата после получения», «Оплата после получения») "
    "— используй enum-значение, если знаешь его, иначе передай лейбл как есть.",
    {
        "client_id": {"type": "integer"},
        "final_price": {"type": "number"},
        "installation_address": {"type": "string"},
        "houses_count": {"type": "integer"},
        "payment_plan": {"type": "string"},
        "advance_amount": {"type": "number"},
    },
    ["client_id"],
    required_module=Module.CLIENTS, domains=[ChatDomain.CLIENTS],
)
def _update_client_documents(db: Session, user: User, client_id: int, **fields) -> dict:
    client = client_service.get_client_or_404(db, client_id)
    if fields.get("payment_plan") is not None:
        try:
            fields["payment_plan"] = parse_payment_plan(fields["payment_plan"])
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    payload = ClientDocumentsUpdate(**{k: v for k, v in fields.items() if v is not None})
    client = client_service.update_documents(db, client, payload)
    return _serialize_client(client)


@register(
    "update_client_payment",
    "Указать, поступил ли платёж для старта производства (полная предоплата или аванс — "
    "в зависимости от формата расчёта клиента).",
    {"client_id": {"type": "integer"}, "is_paid": {"type": "boolean"}}, ["client_id", "is_paid"],
    required_module=Module.CLIENTS, domains=[ChatDomain.CLIENTS],
)
def _update_client_payment(db: Session, user: User, client_id: int, is_paid: bool) -> dict:
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=is_paid))
    return _serialize_client(client)


@register(
    "update_client_balance_payment",
    "Отметить приём остатка «после получения» (для клиентов с форматом «аванс + оплата после "
    "получения» или «оплата после получения», на стадии «постоплата»).",
    {"client_id": {"type": "integer"}, "balance_paid": {"type": "boolean"}}, ["client_id", "balance_paid"],
    required_module=Module.CLIENTS, domains=[ChatDomain.CLIENTS],
)
def _update_client_balance_payment(db: Session, user: User, client_id: int, balance_paid: bool) -> dict:
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.record_balance_payment(
        db, client, ClientBalancePaymentUpdate(balance_paid=balance_paid)
    )
    return _serialize_client(client)


@register(
    "transition_client_stage",
    "Перевести клиента на следующую стадию (лид→обсуждение→согласование→оплата→постоплата). "
    "Требует, чтобы обязательные поля текущей стадии были заполнены.",
    {"client_id": {"type": "integer"}}, ["client_id"],
    required_module=Module.CLIENTS, domains=[ChatDomain.CLIENTS],
)
def _transition_client_stage(db: Session, user: User, client_id: int) -> dict:
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.transition_stage(db, client)
    return _serialize_client(client)


# ------------------------------------------------------------- production --

def _serialize_module(m: ProductionModule) -> dict:
    return {
        "id": m.id,
        "production_id": m.production_id,
        "name": m.name,
        "description": m.description,
        "materials": [
            {
                "id": mm.id,
                "warehouse_material_id": mm.warehouse_material_id,
                "inventory_number": mm.inventory_number,
                "unit": mm.unit,
                "quantity_required": mm.quantity_required,
                "quantity_requested": mm.quantity_requested,
                "quantity_provided": mm.quantity_provided,
            }
            for mm in m.materials
        ],
        "tasks": [{"id": t.id, "title": t.title, "status": t.status.value} for t in m.tasks],
    }


@register(
    "get_production", "Получить производство целиком: все модули дома, их материалы и задачи.",
    {"production_id": {"type": "integer"}}, ["production_id"],
    required_module=Module.PRODUCTION, read_only=True, domains=[ChatDomain.PRODUCTION],
)
def _get_production(db: Session, user: User, production_id: int) -> dict:
    production = production_service.get_production_or_404(db, production_id)
    return {
        "id": production.id,
        "cycle_id": production.cycle_id,
        "modules": [_serialize_module(m) for m in production.modules],
    }


@register(
    "get_module", "Получить один модуль дома: его материалы и задачи.",
    {"module_id": {"type": "integer"}}, ["module_id"],
    required_module=Module.PRODUCTION, read_only=True, domains=[ChatDomain.PRODUCTION],
)
def _get_module(db: Session, user: User, module_id: int) -> dict:
    return _serialize_module(production_service.get_module_or_404(db, module_id))


@register(
    "get_client_context",
    "Получить проектные и документные данные клиента по id цикла - ОБЯЗАТЕЛЬНО вызови перед "
    "созданием нового модуля или задачи по нему, чтобы опираться на пожелания и договор клиента.",
    {"cycle_id": {"type": "integer"}}, ["cycle_id"],
    required_module=Module.PRODUCTION, read_only=True, domains=[ChatDomain.PRODUCTION],
)
def _get_client_context(db: Session, user: User, cycle_id: int) -> dict:
    cycle = db.get(Cycle, cycle_id)
    if not cycle or not cycle.client:
        return {"error": "У этого цикла нет данных клиента"}
    # Production staff need the approved specification, not identity or payment data.
    client = cycle.client
    specs = _attached_specs(client)
    return {
        "cycle_id": cycle.id,
        "client_id": client.id,
        "house_area": client.house_area,
        "wishes_description": client.wishes_description,
        "layout_notes": client.layout_notes,
        "project_locked": client.project_locked_at is not None,
        "house_project_file_id": client.house_project_file_id,
        **specs,
    }


@register(
    "create_module", "Создать новый модуль дома в рамках производства.",
    {"production_id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"}},
    ["production_id", "name"],
    required_module=Module.PRODUCTION, domains=[ChatDomain.PRODUCTION],
)
def _create_module(db: Session, user: User, production_id: int, name: str, description: str | None = None) -> dict:
    module = production_service.create_module(db, production_id, ModuleCreate(name=name, description=description))
    return _serialize_module(module)


@register(
    "create_module_task", "Создать задачу по модулю производства.",
    {
        "module_id": {"type": "integer"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "deadline": {"type": "string", "description": "ISO 8601 дата/время"},
        "assignee_ids": {"type": "array", "items": {"type": "integer"}},
        "reviewer_ids": {"type": "array", "items": {"type": "integer"}},
        "depends_on_ids": {"type": "array", "items": {"type": "integer"}},
    },
    ["module_id", "title"],
    required_module=Module.PRODUCTION, domains=[ChatDomain.PRODUCTION],
)
def _create_module_task(
    db: Session, user: User, module_id: int, title: str, description: str | None = None,
    deadline: str | None = None, assignee_ids: list[int] | None = None,
    reviewer_ids: list[int] | None = None, depends_on_ids: list[int] | None = None,
) -> dict:
    task = task_service.create_task(
        db,
        title=title,
        description=description,
        deadline=datetime.fromisoformat(deadline) if deadline else None,
        assignee_ids=assignee_ids or [],
        reviewer_ids=reviewer_ids or [],
        depends_on_ids=depends_on_ids or [],
        module_id=module_id,
    )
    return {"id": task.id, "title": task.title, "status": task.status.value}


@register(
    "request_material", "Запросить материал со склада для материала модуля (module_material_id).",
    {"module_material_id": {"type": "integer"}, "quantity": {"type": "number"}},
    ["module_material_id", "quantity"],
    required_module=Module.PRODUCTION, domains=[ChatDomain.PRODUCTION],
)
def _request_material(db: Session, user: User, module_material_id: int, quantity: float) -> dict:
    material = production_service.get_module_material_or_404(db, module_material_id)
    request = production_service.request_material(db, material, quantity, requested_by=user)
    return {"request_id": request.id, "quantity": request.quantity, "status": request.status.value}


@register(
    "add_module_material", "Добавить в модуль запись о необходимом материале со склада.",
    {
        "module_id": {"type": "integer"},
        "warehouse_material_id": {"type": "integer"},
        "inventory_number": {"type": "string"},
        "unit": {"type": "string"},
        "quantity_required": {"type": "number"},
    },
    ["module_id", "warehouse_material_id", "inventory_number", "unit", "quantity_required"],
    required_module=Module.PRODUCTION, domains=[ChatDomain.PRODUCTION],
)
def _add_module_material(db: Session, user: User, module_id: int, **fields) -> dict:
    material = production_service.add_module_material(db, module_id, ModuleMaterialCreate(**fields))
    return {"id": material.id, "warehouse_material_id": material.warehouse_material_id}


# ------------------------------------------------------------------ cycle --

@register(
    "get_cycle", "Получить цикл клиента целиком: клиент + производство + монтаж + текущий статус.",
    {"cycle_id": {"type": "integer"}}, ["cycle_id"],
    required_module=Module.CYCLE, read_only=True, domains=[ChatDomain.CYCLE],
)
def _get_cycle(db: Session, user: User, cycle_id: int) -> dict:
    cycle = db.get(Cycle, cycle_id)
    if not cycle:
        return {"error": "Цикл не найден"}
    return {
        "id": cycle.id,
        "status": cycle.status.value,
        "client": _serialize_client(cycle.client) if cycle.client else None,
        "productions": [
            {
                "id": p.id,
                "house_index": p.house_index,
                "name": p.name,
                "modules": [_serialize_module(m) for m in p.modules],
            }
            for p in cycle.productions
        ],
        "installation": {
            "id": cycle.installation.id,
            "stage": cycle.installation.stage.value,
            "address": cycle.installation.address,
            "scheduled_date": _iso(cycle.installation.scheduled_date),
        } if cycle.installation else None,
    }


@register(
    "list_cycles", "Список всех циклов клиентов с их текущим статусом.",
    {"status": {"type": "string", "enum": ["client", "production", "installation", "completed"]}},
    required_module=Module.CYCLE, read_only=True, domains=[ChatDomain.CYCLE],
)
def _list_cycles(db: Session, user: User, status: str | None = None) -> dict:
    query = db.query(Cycle)
    if status:
        query = query.filter(Cycle.status == status)
    cycles = query.order_by(Cycle.id.desc()).limit(50).all()
    return {
        "cycles": [
            {"id": c.id, "status": c.status.value, "client_name": c.client.full_name if c.client else None}
            for c in cycles
        ]
    }


# -------------------------------------------------------------- warehouse --

@register(
    "get_material", "Получить информацию о материале склада (остаток, запрошено, требуется поставка).",
    {"material_id": {"type": "integer"}}, ["material_id"],
    required_module=Module.WAREHOUSE, read_only=True, domains=[ChatDomain.WAREHOUSE],
)
def _get_material(db: Session, user: User, material_id: int) -> dict:
    material = warehouse_service.get_material_or_404(db, material_id)
    return warehouse_service.to_out(db, material).model_dump()


@register(
    "list_materials", "Список материалов склада, опционально только те, что требуют поставки.",
    {"needs_supply": {"type": "boolean"}},
    required_module=Module.WAREHOUSE, read_only=True, domains=[ChatDomain.WAREHOUSE],
)
def _list_materials(db: Session, user: User, needs_supply: bool = False) -> dict:
    materials = warehouse_service.list_materials(db, only_needs_supply=needs_supply)
    return {"materials": [m.model_dump() for m in materials]}


@register(
    "list_pending_requests", "Список необработанных заявок на материалы от производства.",
    {},
    required_module=Module.WAREHOUSE, read_only=True, domains=[ChatDomain.WAREHOUSE],
)
def _list_pending_requests(db: Session, user: User) -> dict:
    requests = (
        db.query(MaterialRequest).filter(MaterialRequest.status == MaterialRequestStatus.PENDING).all()
    )
    return {
        "requests": [
            {
                "id": r.id,
                "warehouse_material_id": r.warehouse_material_id,
                "material_title": r.warehouse_material.title,
                "quantity": r.quantity,
                "module_id": r.module_material.module_id,
            }
            for r in requests
        ]
    }


@register(
    "update_threshold", "Изменить пороговое значение материала (ниже которого требуется поставка).",
    {"material_id": {"type": "integer"}, "threshold": {"type": "number"}}, ["material_id", "threshold"],
    required_module=Module.WAREHOUSE, domains=[ChatDomain.WAREHOUSE],
)
def _update_threshold(db: Session, user: User, material_id: int, threshold: float) -> dict:
    material = warehouse_service.get_material_or_404(db, material_id)
    material = warehouse_service.update_material(db, material, WarehouseMaterialUpdate(threshold=threshold))
    return warehouse_service.to_out(db, material).model_dump()


@register(
    "approve_request", "Одобрить заявку на материал от производства.",
    {"request_id": {"type": "integer"}}, ["request_id"],
    required_module=Module.WAREHOUSE, domains=[ChatDomain.WAREHOUSE],
)
def _approve_request(db: Session, user: User, request_id: int) -> dict:
    request = warehouse_service.get_request_or_404(db, request_id)
    request = warehouse_service.approve_request(db, request, decided_by=user)
    return {"request_id": request.id, "status": request.status.value}


@register(
    "reject_request", "Отклонить заявку на материал от производства.",
    {"request_id": {"type": "integer"}}, ["request_id"],
    required_module=Module.WAREHOUSE, domains=[ChatDomain.WAREHOUSE],
)
def _reject_request(db: Session, user: User, request_id: int) -> dict:
    request = warehouse_service.get_request_or_404(db, request_id)
    request = warehouse_service.reject_request(db, request, decided_by=user)
    return {"request_id": request.id, "status": request.status.value}


@register(
    "create_supply", "Записать поставку материалов на склад (увеличивает остаток).",
    {
        "supplier_name": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "warehouse_material_id": {"type": "integer"},
                    "quantity": {"type": "number"},
                },
                "required": ["warehouse_material_id", "quantity"],
            },
        },
    },
    ["lines"],
    required_module=Module.WAREHOUSE, domains=[ChatDomain.WAREHOUSE],
)
def _create_supply(db: Session, user: User, lines: list[dict], supplier_name: str | None = None) -> dict:
    payload = SupplyCreate(supplier_name=supplier_name, lines=[SupplyLineCreate(**line) for line in lines])
    supply = warehouse_service.create_supply(db, payload, created_by=user)
    return {"supply_id": supply.id, "lines": len(supply.lines)}


# -------------------------------------------------------------- marketing --

def _serialize_content(c: ContentItem) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "description": c.description,
        "planned_release_date": _iso(c.planned_release_date),
        "platforms": c.platforms,
        "stage": c.stage.value,
        "raw_texts": c.raw_texts,
        "raw_locked": c.raw_locked_at is not None,
        "final_texts": c.final_texts,
        "post_links": [{"platform": p.platform, "url": p.url} for p in c.post_links],
        "analysis_notes": c.analysis_notes,
        "analysis_reach": c.analysis_reach,
    }


@register(
    "get_content", "Получить единицу контента маркетинга целиком.",
    {"content_id": {"type": "integer"}}, ["content_id"],
    required_module=Module.MARKETING, read_only=True, domains=[ChatDomain.MARKETING],
)
def _get_content(db: Session, user: User, content_id: int) -> dict:
    return _serialize_content(marketing_service.get_content_or_404(db, content_id))


@register(
    "list_content", "Список всех единиц контента маркетинга.",
    {},
    required_module=Module.MARKETING, read_only=True, domains=[ChatDomain.MARKETING],
)
def _list_content(db: Session, user: User) -> dict:
    items = db.query(ContentItem).order_by(ContentItem.id.desc()).limit(50).all()
    return {"content": [{"id": c.id, "title": c.title, "stage": c.stage.value} for c in items]}


@register(
    "create_content", "Создать новую единицу контента (идея поста/публикации).",
    {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "planned_release_date": {"type": "string", "description": "ISO 8601 дата"},
        "platforms": {"type": "array", "items": {"type": "string"}},
        "assignee_ids": {"type": "array", "items": {"type": "integer"}},
    },
    ["title"],
    required_module=Module.MARKETING, domains=[ChatDomain.MARKETING],
)
def _create_content(
    db: Session, user: User, title: str, description: str | None = None,
    planned_release_date: str | None = None, platforms: list[str] | None = None,
    assignee_ids: list[int] | None = None,
) -> dict:
    content = marketing_service.create_content(
        db,
        ContentItemCreate(
            title=title,
            description=description,
            planned_release_date=date.fromisoformat(planned_release_date) if planned_release_date else None,
            platforms=platforms or [],
            assignee_ids=assignee_ids or [],
        ),
    )
    return _serialize_content(content)


@register(
    "write_content_text",
    "Написать сырой или готовый текст материала. Ты сам сочиняешь текст поста в аргументе text.",
    {
        "content_id": {"type": "integer"},
        "stage": {"type": "string", "enum": ["raw", "final"]},
        "text": {"type": "string"},
    },
    ["content_id", "stage", "text"],
    required_module=Module.MARKETING, domains=[ChatDomain.MARKETING],
)
def _write_content_text(db: Session, user: User, content_id: int, stage: str, text: str) -> dict:
    content = marketing_service.get_content_or_404(db, content_id)
    if stage == "raw":
        content = marketing_service.update_raw(db, content, ContentRawUpdate(raw_texts=text))
    else:
        content = marketing_service.update_final(db, content, ContentFinalUpdate(final_texts=text))
    return _serialize_content(content)


@register(
    "write_analysis",
    "Записать анализ результатов публикации на основе уже сохранённых охватов (посмотри их через get_content).",
    {
        "content_id": {"type": "integer"},
        "notes": {"type": "string"},
        "reach": {"type": "object", "description": "охваты по платформам, {платформа: число}"},
    },
    ["content_id"],
    required_module=Module.MARKETING, domains=[ChatDomain.MARKETING],
)
def _write_analysis(db: Session, user: User, content_id: int, notes: str | None = None, reach: dict | None = None) -> dict:
    content = marketing_service.get_content_or_404(db, content_id)
    content = marketing_service.update_analysis(db, content, ContentAnalysisUpdate(analysis_notes=notes, analysis_reach=reach))
    return _serialize_content(content)


@register(
    "transition_content", "Перевести единицу контента на следующую стадию.",
    {"content_id": {"type": "integer"}}, ["content_id"],
    required_module=Module.MARKETING, domains=[ChatDomain.MARKETING],
)
def _transition_content(db: Session, user: User, content_id: int) -> dict:
    content = marketing_service.get_content_or_404(db, content_id)
    content = marketing_service.transition_stage(db, content)
    return _serialize_content(content)


# ------------------------------------------------------------------ tasks --

def _serialize_task(t: Task) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "deadline": _iso(t.deadline),
        "status": t.status.value,
        "module_id": t.module_id,
        "assignees": [{"id": u.id, "full_name": u.full_name} for u in t.assignees],
        "reviewers": [{"id": u.id, "full_name": u.full_name} for u in t.reviewers],
    }


@register(
    "get_task", "Получить задачу целиком.",
    {"task_id": {"type": "integer"}}, ["task_id"],
    required_module=Module.TASKS, read_only=True, domains=[ChatDomain.TASKS, ChatDomain.PRODUCTION],
)
def _get_task(db: Session, user: User, task_id: int) -> dict:
    return _serialize_task(task_service.get_task_or_404(db, task_id))


@register(
    "list_tasks", "Список задач с фильтрами.",
    {
        "assignee_id": {"type": "integer"},
        "status": {"type": "string", "enum": [s.value for s in TaskStatus]},
        "module_id": {"type": "integer"},
    },
    required_module=Module.TASKS, read_only=True, domains=[ChatDomain.TASKS],
)
def _list_tasks(db: Session, user: User, assignee_id: int | None = None, status: str | None = None, module_id: int | None = None) -> dict:
    query = db.query(Task)
    if assignee_id is not None:
        query = query.filter(Task.assignees.any(User.id == assignee_id))
    if status is not None:
        query = query.filter(Task.status == TaskStatus(status))
    if module_id is not None:
        query = query.filter(Task.module_id == module_id)
    tasks = query.order_by(Task.id.desc()).limit(50).all()
    return {"tasks": [_serialize_task(t) for t in tasks]}


@register(
    "get_user_workload",
    "Загрузка сотрудников: сколько у каждого сейчас незавершённых задач. Используй это, чтобы "
    "предложить, кому поручить новую задачу, вместо угадывания.",
    {"module": {"type": "string", "enum": [m.value for m in Module]}},
    required_module=Module.TASKS, read_only=True, domains=[ChatDomain.TASKS],
)
def _get_user_workload(db: Session, user: User, module: str | None = None) -> dict:
    if module:
        candidates = user_service.users_with_access(db, Module(module))
    else:
        candidates = db.query(User).filter(User.is_active.is_(True)).all()
    result = []
    for u in candidates:
        open_count = (
            db.query(Task)
            .filter(Task.assignees.any(User.id == u.id), Task.status != TaskStatus.DONE)
            .count()
        )
        result.append({"id": u.id, "full_name": u.full_name, "open_tasks": open_count})
    return {"workload": sorted(result, key=lambda r: r["open_tasks"])}


@register(
    "accept_task",
    "Принять задачу в работу от своего имени (перевод «готова к работе» → «в работе»). "
    "Сработает только если текущий пользователь — исполнитель этой задачи.",
    {"task_id": {"type": "integer"}}, ["task_id"],
    required_module=Module.TASKS, domains=[ChatDomain.TASKS, ChatDomain.PRODUCTION],
)
def _accept_task(db: Session, user: User, task_id: int) -> dict:
    task = task_service.get_task_or_404(db, task_id)
    task = task_service.set_status(db, task, TaskStatus.IN_PROGRESS, actor=user)
    return _serialize_task(task)


from app.ai import live_tools as _live_tools  # noqa: E402,F401
