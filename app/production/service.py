from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.cycle.models import CycleStatus
from app.production.models import MaterialRequest, MaterialRequestStatus, ModuleMaterial, Production, ProductionModule
from app.production.schemas import ModuleCreate, ModuleMaterialCreate, ModuleUpdate
from app.tasks import service as task_service
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.users import service as user_service
from app.warehouse import service as warehouse_service
from app.warehouse.models import StockMovementReason, WarehouseMaterial


def list_productions(db: Session, cycle_id: int | None = None) -> list[Production]:
    query = db.query(Production)
    if cycle_id is not None:
        query = query.filter(Production.cycle_id == cycle_id)
    return query.order_by(Production.cycle_id.desc(), Production.house_index.asc()).all()


def get_production_or_404(db: Session, production_id: int) -> Production:
    production = db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Производство не найдено")
    return production


def get_module_or_404(db: Session, module_id: int) -> ProductionModule:
    module = db.get(ProductionModule, module_id)
    if not module:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Модуль не найден")
    return module


def get_module_material_or_404(db: Session, module_material_id: int) -> ModuleMaterial:
    material = db.get(ModuleMaterial, module_material_id)
    if not material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Материал модуля не найден")
    return material


def delete_production(db: Session, production: Production) -> None:
    """Удалить производство целиком (каскад на модули/материалы/заявки —
    `ondelete=CASCADE` в БД). Отказ 409, если цикл ещё не завершён или есть
    незавершённые заявки на материалы/задачи по его модулям — по умолчанию
    запрет, а не тихий каскад (см. спеку 0030-b)."""
    if production.cycle.status != CycleStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить производство: цикл ещё не завершён",
        )
    module_ids = [m.id for m in production.modules]
    if module_ids:
        pending = (
            db.query(MaterialRequest.id)
            .join(ModuleMaterial, MaterialRequest.module_material_id == ModuleMaterial.id)
            .filter(
                ModuleMaterial.module_id.in_(module_ids),
                MaterialRequest.status == MaterialRequestStatus.PENDING,
            )
            .first()
        )
        if pending is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Нельзя удалить производство: есть незавершённые заявки на материалы",
            )
        open_task = (
            db.query(Task.id).filter(Task.module_id.in_(module_ids), Task.status != TaskStatus.DONE).first()
        )
        if open_task is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Нельзя удалить производство: есть незавершённые задачи по модулям",
            )
        # Завершённые задачи остаются как история — снимаем ссылку на модуль,
        # который вот-вот исчезнет (у tasks.module_id нет ondelete в БД).
        db.query(Task).filter(Task.module_id.in_(module_ids)).update(
            {"module_id": None}, synchronize_session="fetch"
        )
    db.delete(production)
    db.flush()


def delete_module(db: Session, module: ProductionModule) -> None:
    """Удалить один модуль дома. Отказ 409, если по модулю уже выдавались
    материалы со склада (реальная работа началась) или есть незавершённая
    заявка/задача — по умолчанию запрет, а не тихий каскад."""
    if any(float(m.quantity_provided) > 0 for m in module.materials):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить модуль: по нему уже выдавались материалы со склада",
        )
    pending = (
        db.query(MaterialRequest.id)
        .join(ModuleMaterial, MaterialRequest.module_material_id == ModuleMaterial.id)
        .filter(ModuleMaterial.module_id == module.id, MaterialRequest.status == MaterialRequestStatus.PENDING)
        .first()
    )
    if pending is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить модуль: есть незавершённая заявка на материалы",
        )
    open_task = db.query(Task.id).filter(Task.module_id == module.id, Task.status != TaskStatus.DONE).first()
    if open_task is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить модуль: есть незавершённые задачи",
        )
    db.query(Task).filter(Task.module_id == module.id).update({"module_id": None}, synchronize_session="fetch")
    db.delete(module)
    db.flush()


def create_module(db: Session, production_id: int, payload: ModuleCreate) -> ProductionModule:
    get_production_or_404(db, production_id)
    module = ProductionModule(production_id=production_id, name=payload.name, description=payload.description)
    db.add(module)
    db.flush()
    return module


def update_module(db: Session, module: ProductionModule, payload: ModuleUpdate) -> ProductionModule:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(module, field, value)
    db.flush()
    return module


def add_module_material(db: Session, module_id: int, payload: ModuleMaterialCreate) -> ModuleMaterial:
    get_module_or_404(db, module_id)
    warehouse_material = db.get(WarehouseMaterial, payload.warehouse_material_id)
    if not warehouse_material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Материал склада не найден")
    material = ModuleMaterial(
        module_id=module_id,
        warehouse_material_id=payload.warehouse_material_id,
        inventory_number=payload.inventory_number,
        unit=payload.unit,
        quantity_required=payload.quantity_required,
        quantity_requested=0,
        quantity_provided=0,
    )
    db.add(material)
    db.flush()
    return material


def update_required_quantity(
    db: Session, material: ModuleMaterial, quantity_required: float, actor
) -> ModuleMaterial:
    if quantity_required < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Количество не может быть отрицательным")
    diff = quantity_required - float(material.quantity_required)
    material.quantity_required = quantity_required
    db.flush()
    if diff > 0:
        warehouse_service.log_movement(
            db, material.warehouse_material, diff, StockMovementReason.REQUIRED_ADJUSTED_UP, actor, material.id
        )
    return material


def request_material(db: Session, material: ModuleMaterial, quantity: float, requested_by) -> MaterialRequest:
    if quantity <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Количество должно быть положительным")
    if quantity > material.quantity_required:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя запросить больше, чем указано в поле «необходимо»",
        )

    material.quantity_required -= quantity
    material.quantity_requested += quantity
    db.flush()

    request = MaterialRequest(
        module_material_id=material.id,
        warehouse_material_id=material.warehouse_material_id,
        quantity=quantity,
        status=MaterialRequestStatus.PENDING,
        requested_by_id=requested_by.id,
    )
    db.add(request)
    db.flush()

    assignees = user_service.users_with_access(db, AccessModule.WAREHOUSE)
    task = task_service.create_link_task(
        db,
        title=f"Заявка на материал «{material.warehouse_material.title}» ({quantity} {material.unit}) "
        f"для модуля «{material.module.name}»",
        link_type=TaskLinkType.WAREHOUSE_REQUEST,
        link_id=request.id,
        assignees=assignees,
    )
    request.task_id = task.id
    db.flush()
    return request
