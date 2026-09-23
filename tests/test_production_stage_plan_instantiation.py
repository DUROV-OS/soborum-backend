"""Применение шаблона к производству + автозапуск на переходе цикла (0066-f):

- instantiate_stage_plan — идемпотентно превращает подтверждённый шаблон в
  реальные блоки/задачи/материалы, с зависимостями между блоками и задачами;
- переход клиента «оплата» → «постоплата» сам применяет уже подтверждённый
  шаблон той же модели дома, без ручного действия;
- клиент новой модели (шаблона ещё нет) — переход не падает, просто не
  создаёт блоков (ждут проверки [[0066-e]]);
- мультидом — план применяется к каждому дому, шаблон переиспользуется.
"""

from app.clients import service as client_service
from app.clients.models import Client, ClientStage, OrderType, PaymentPlan
from app.clients.schemas import ClientCreate, ClientDocumentsUpdate, ClientHousesCountUpdate, ClientPaymentUpdate
from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.house_models.models import HouseModelCard, HouseModelConfirmation, HouseModelKind
from app.production import stage_plan
from app.production.models import Production, ProductionBlock
from app.production.stage_templates import (
    ProductionStageTemplate,
    TemplateBlock,
    TemplateBlockMaterial,
    TemplateBlockTask,
    TemplateStatus,
)
from app.tasks.models import Task, TaskStatus
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial


def _make_house_model(db, key="DH-64"):
    model = HouseModelCard(
        key=key, title=key, kind=HouseModelKind.CATALOG,
        confirmation=HouseModelConfirmation.NONE, confirmation_label="Не подтверждено",
        source_note_path="test/model.md",
    )
    db.add(model)
    db.flush()
    return model


def _make_client_at_payment(db, house_model_key=None, houses_count=1, admin=None):
    client = client_service.create_client(
        db, ClientCreate(full_name="Клиент 0066-f", phone="+79160000000", email="f@example.com")
    )
    client_service.transition_stage(db, client)  # LEAD -> DISCUSSION
    client_service.transition_stage(db, client)  # DISCUSSION -> SITE_VISIT
    client_service.transition_stage(db, client)  # SITE_VISIT -> APPROVAL
    client.contract_file_id = 1
    client.contract_appendix_file_id = 1
    client.ar_file_id = 1
    client.kr_file_id = 1
    order_type = OrderType.MULTIPLE if houses_count > 1 else OrderType.SINGLE
    client_service.update_documents(db, client, ClientDocumentsUpdate(
        order_type=order_type, house_model_key=house_model_key,
        final_price=2_000_000, installation_address="г. Тест, ул. Тест, 1",
        payment_plan=PaymentPlan.POST_PAYMENT,
    ))
    if houses_count > 1:
        client_service.update_houses_count(db, client, ClientHousesCountUpdate(houses_count=houses_count))
    client_service.transition_stage(db, client)  # APPROVAL -> PAYMENT
    db.commit()
    return client


def _make_confirmed_template(db, house_model_key, source_client_id, admin_id):
    template = ProductionStageTemplate(
        house_model_key=house_model_key, status=TemplateStatus.CONFIRMED,
        source_client_id=source_client_id, confirmed_by_id=admin_id,
    )
    db.add(template)
    db.flush()

    warehouse_material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title="Свая винтовая",
        code="M-SVAYA", unit="шт", quantity_in_stock=100,
    )
    db.add(warehouse_material)
    db.flush()

    foundation = TemplateBlock(
        template_id=template.id, name="Фундамент", sequence=1, kr_page_refs=[{"page_number": 3, "note": None}],
    )
    frame = TemplateBlock(
        template_id=template.id, name="Каркас", sequence=2, kr_page_refs=[{"page_number": 7, "note": None}],
    )
    db.add_all([foundation, frame])
    db.flush()
    frame.depends_on = [foundation]

    db.add(TemplateBlockTask(
        template_block_id=foundation.id, title="Завинтить сваи",
        kr_page_ref={"page_number": 3, "note": None},
    ))
    db.add(TemplateBlockTask(
        template_block_id=frame.id, title="Собрать каркас",
        kr_page_ref={"page_number": 7, "note": None},
    ))
    db.add(TemplateBlockMaterial(
        template_block_id=foundation.id, name="Свая винтовая", unit="шт",
        kr_page_ref={"page_number": 3, "note": None}, warehouse_material_id=warehouse_material.id,
    ))
    db.add(TemplateBlockMaterial(
        template_block_id=frame.id, name="Брус 150х150", unit="м3",
        kr_page_ref={"page_number": 7, "note": None}, warehouse_material_id=None,
    ))
    db.flush()
    return template


def _make_production(db, cycle_status=CycleStatus.PRODUCTION):
    cycle = Cycle(status=cycle_status)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    return production


def test_instantiate_creates_blocks_tasks_materials_with_dependencies(db, make_user):
    admin = make_user(admin=True)
    worker = make_user(Module.PRODUCTION)
    production = _make_production(db)
    template = _make_confirmed_template(db, house_model_key=None, source_client_id=1, admin_id=admin.id)
    db.commit()

    stage_plan.instantiate_stage_plan(db, production, template)
    db.commit()

    blocks = db.query(ProductionBlock).filter(ProductionBlock.production_id == production.id).all()
    assert len(blocks) == 2
    foundation = next(b for b in blocks if b.name == "Фундамент")
    frame = next(b for b in blocks if b.name == "Каркас")
    assert frame.depends_on_ids == [foundation.id]
    assert foundation.sequence == 1 and frame.sequence == 2

    assert len(foundation.materials) == 1
    assert foundation.materials[0].quantity_required == 0

    foundation_tasks = [t for t in foundation.tasks]
    frame_tasks = [t for t in frame.tasks]
    assert len(foundation_tasks) == 1
    # Каркас: собственная задача + задача на донаполнение материала без warehouse_material_id.
    assert len(frame_tasks) == 2
    real_frame_task = next(t for t in frame_tasks if t.title == "Собрать каркас")
    assert real_frame_task.status == TaskStatus.NOT_READY  # ждёт задачу фундамента
    assert [t.id for t in real_frame_task.depends_on] == [foundation_tasks[0].id]
    assert real_frame_task.link_meta == {"kr_page": 7}
    assert worker.id in [u.id for u in foundation_tasks[0].assignees]

    unresolved_material_task = next(t for t in frame_tasks if "Сопоставить со складом" in t.title)
    assert "Брус 150х150" in unresolved_material_task.title


def test_instantiate_is_idempotent(db, make_user):
    admin = make_user(admin=True)
    make_user(Module.PRODUCTION)
    production = _make_production(db)
    template = _make_confirmed_template(db, house_model_key=None, source_client_id=1, admin_id=admin.id)
    db.commit()

    stage_plan.instantiate_stage_plan(db, production, template)
    db.commit()
    first_count = db.query(ProductionBlock).filter(ProductionBlock.production_id == production.id).count()

    stage_plan.instantiate_stage_plan(db, production, template)
    db.commit()
    second_count = db.query(ProductionBlock).filter(ProductionBlock.production_id == production.id).count()

    assert first_count == second_count == 2


def test_transition_to_postpayment_applies_confirmed_template_automatically(db, make_user):
    admin = make_user(admin=True)
    worker = make_user(Module.PRODUCTION)
    _make_house_model(db, "DH-64")
    source_client = client_service.create_client(
        db, ClientCreate(full_name="Источник шаблона", phone="+70000000001", email="src@example.com")
    )
    db.flush()
    _make_confirmed_template(db, house_model_key="DH-64", source_client_id=source_client.id, admin_id=admin.id)
    db.commit()

    client = _make_client_at_payment(db, house_model_key="DH-64")
    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=False))
    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT
    db.commit()

    assert client.stage == ClientStage.POSTPAYMENT
    production = db.query(Production).filter(Production.cycle_id == client.cycle_id).one()
    assert len(production.blocks) == 2
    assert worker  # исполнитель по умолчанию — участник Module.PRODUCTION


def test_transition_leaves_production_without_blocks_when_no_template_yet(db, make_user):
    make_user(Module.PRODUCTION)
    _make_house_model(db, "DH-99")
    client = _make_client_at_payment(db, house_model_key="DH-99")
    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=False))

    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT — не должен упасть
    db.commit()

    assert client.stage == ClientStage.POSTPAYMENT
    production = db.query(Production).filter(Production.cycle_id == client.cycle_id).one()
    assert production.blocks == []


def test_multi_house_applies_confirmed_template_to_each_house(db, make_user):
    admin = make_user(admin=True)
    make_user(Module.PRODUCTION)
    _make_house_model(db, "DH-70")
    source_client = client_service.create_client(
        db, ClientCreate(full_name="Источник шаблона 2", phone="+70000000002", email="src2@example.com")
    )
    db.flush()
    _make_confirmed_template(db, house_model_key="DH-70", source_client_id=source_client.id, admin_id=admin.id)
    db.commit()

    client = _make_client_at_payment(db, house_model_key="DH-70", houses_count=2)
    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=False))
    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT
    db.commit()

    productions = db.query(Production).filter(Production.cycle_id == client.cycle_id).order_by(Production.house_index).all()
    assert len(productions) == 2
    assert all(len(p.blocks) == 2 for p in productions)
