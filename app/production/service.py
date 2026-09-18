from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.cycle.models import CycleStatus
from app.production.models import BlockMaterial, MaterialRequest, MaterialRequestStatus, Production, ProductionBlock
from app.production.schemas import BlockCreate, BlockMaterialCreate, BlockUpdate
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


def completed_flags_by_production(db: Session, production_ids: list[int]) -> dict[int, bool]:
    """Признак завершённости для списка производств одним проходом по их
    блокам/задачам — тот же факт («у блока нет открытых задач»), что уже
    используют guard-проверки удаления (`delete_production`/`delete_block`
    выше), просто batch'ем по многим производствам для `/production/` списка.
    Производство без единого блока считается не завершённым."""
    completed: dict[int, bool] = {pid: False for pid in production_ids}
    if not production_ids:
        return completed

    blocks = (
        db.query(ProductionBlock.id, ProductionBlock.production_id)
        .filter(ProductionBlock.production_id.in_(production_ids))
        .all()
    )
    if not blocks:
        return completed
    production_by_block = {b.id: b.production_id for b in blocks}
    block_ids = list(production_by_block.keys())

    productions_with_open_task = {
        production_by_block[block_id]
        for (block_id,) in db.query(Task.block_id)
        .filter(Task.block_id.in_(block_ids), Task.status != TaskStatus.DONE)
        .all()
    }
    for production_id in set(production_by_block.values()):
        completed[production_id] = production_id not in productions_with_open_task
    return completed


def get_production_or_404(db: Session, production_id: int) -> Production:
    production = db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Производство не найдено")
    return production


def get_block_or_404(db: Session, block_id: int) -> ProductionBlock:
    block = db.get(ProductionBlock, block_id)
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Блок не найден")
    return block


def get_block_material_or_404(db: Session, block_material_id: int) -> BlockMaterial:
    material = db.get(BlockMaterial, block_material_id)
    if not material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Материал блока не найден")
    return material


def delete_production(db: Session, production: Production) -> None:
    """Удалить производство целиком (каскад на блоки/материалы/заявки —
    `ondelete=CASCADE` в БД). Отказ 409, если цикл ещё не завершён или есть незавершённые
    заявки на материалы/задачи по его блокам — по умолчанию запрет, а не тихий каскад
    (см. спеку 0030-b)."""
    if production.cycle.status != CycleStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить производство: цикл ещё не завершён",
        )
    block_ids = [b.id for b in production.blocks]
    if block_ids:
        pending = (
            db.query(MaterialRequest.id)
            .join(BlockMaterial, MaterialRequest.block_material_id == BlockMaterial.id)
            .filter(
                BlockMaterial.block_id.in_(block_ids),
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
            db.query(Task.id).filter(Task.block_id.in_(block_ids), Task.status != TaskStatus.DONE).first()
        )
        if open_task is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Нельзя удалить производство: есть незавершённые задачи по блокам",
            )
        # Завершённые задачи остаются как история — снимаем ссылку на блок,
        # который вот-вот исчезнет (у tasks.block_id нет ondelete в БД).
        db.query(Task).filter(Task.block_id.in_(block_ids)).update(
            {"block_id": None}, synchronize_session="fetch"
        )
    db.delete(production)
    db.flush()


def delete_block(db: Session, block: ProductionBlock) -> None:
    """Удалить один блок производства. Отказ 409, если по блоку уже выдавались
    материалы со склада (реальная работа началась) или есть незавершённая
    заявка/задача — по умолчанию запрет, а не тихий каскад."""
    if any(float(m.quantity_provided) > 0 for m in block.materials):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить блок: по нему уже выдавались материалы со склада",
        )
    pending = (
        db.query(MaterialRequest.id)
        .join(BlockMaterial, MaterialRequest.block_material_id == BlockMaterial.id)
        .filter(BlockMaterial.block_id == block.id, MaterialRequest.status == MaterialRequestStatus.PENDING)
        .first()
    )
    if pending is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить блок: есть незавершённая заявка на материалы",
        )
    open_task = db.query(Task.id).filter(Task.block_id == block.id, Task.status != TaskStatus.DONE).first()
    if open_task is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить блок: есть незавершённые задачи",
        )
    db.query(Task).filter(Task.block_id == block.id).update({"block_id": None}, synchronize_session="fetch")
    db.delete(block)
    db.flush()


def create_block(db: Session, production_id: int, payload: BlockCreate) -> ProductionBlock:
    get_production_or_404(db, production_id)
    sequence = payload.sequence
    if sequence is None:
        sequence = (db.query(ProductionBlock).filter(ProductionBlock.production_id == production_id).count()) + 1
    block = ProductionBlock(
        production_id=production_id, name=payload.name, description=payload.description, sequence=sequence
    )
    db.add(block)
    db.flush()
    return block


def update_block(db: Session, block: ProductionBlock, payload: BlockUpdate) -> ProductionBlock:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(block, field, value)
    db.flush()
    return block


def _depends_transitively_on(block: ProductionBlock, target_id: int, seen: set[int] | None = None) -> bool:
    """DFS: истина, если `block` (прямо или через цепочку) уже зависит от блока
    `target_id` — используется, чтобы не дать замкнуть зависимости в цикл."""
    if seen is None:
        seen = set()
    if block.id in seen:
        return False
    seen.add(block.id)
    for dep in block.depends_on:
        if dep.id == target_id:
            return True
        if _depends_transitively_on(dep, target_id, seen):
            return True
    return False


def add_block_dependency(db: Session, block: ProductionBlock, depends_on_id: int) -> ProductionBlock:
    if depends_on_id == block.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Блок не может зависеть от самого себя")
    depends_on = get_block_or_404(db, depends_on_id)
    if depends_on.production_id != block.production_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Зависимость должна быть в рамках того же производства"
        )
    if depends_on.id in {b.id for b in block.depends_on}:
        return block
    # Если target уже (транзитивно) зависит от block — добавление создаст цикл.
    if _depends_transitively_on(depends_on, block.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Эта зависимость замкнула бы блоки в цикл"
        )
    block.depends_on.append(depends_on)
    db.flush()
    return block


def remove_block_dependency(db: Session, block: ProductionBlock, depends_on_id: int) -> ProductionBlock:
    block.depends_on = [b for b in block.depends_on if b.id != depends_on_id]
    db.flush()
    return block


def add_block_material(db: Session, block_id: int, payload: BlockMaterialCreate) -> BlockMaterial:
    get_block_or_404(db, block_id)
    warehouse_material = db.get(WarehouseMaterial, payload.warehouse_material_id)
    if not warehouse_material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Материал склада не найден")
    material = BlockMaterial(
        block_id=block_id,
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
    db: Session, material: BlockMaterial, quantity_required: float, actor
) -> BlockMaterial:
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


def request_material(db: Session, material: BlockMaterial, quantity: float, requested_by) -> MaterialRequest:
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
        block_material_id=material.id,
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
        f"для блока «{material.block.name}»",
        link_type=TaskLinkType.WAREHOUSE_REQUEST,
        link_id=request.id,
        assignees=assignees,
    )
    request.task_id = task.id
    db.flush()
    return request
