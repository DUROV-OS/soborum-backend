"""Раздел «Бухгалтерия» в детерминированной сводке «Пульса»
(app.dashboard.overview) — черновики проводок как реальный, не выдуманный
сигнал внимания (используется разделом «Работа» вместо замоканного совета).
"""

from sqlalchemy import event

from app.accounting.schemas import MoneyMovementCreate
from app.accounting.service import create_money_movement
from app.common.module_access import Module
from app.dashboard.overview import generate_section_signal, generate_today
from app.dashboard.service import SECTION_BUILDERS, _snapshot_warehouse
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial


def test_accounting_widget_and_action_reflect_real_drafts(db, make_user):
    user = make_user(Module.ACCOUNTING, admin=True)
    employee = make_user()

    out = generate_today(db, user)
    assert not any(a.section == "accounting" for a in out.actions)

    create_money_movement(
        db,
        MoneyMovementCreate(subkind="salary_payout", amount=1000, employee_id=employee.id),
        initiator_id=user.id,
    )

    out = generate_today(db, user)
    widget = next(w for w in out.widgets if w.section == "accounting" and w.title == "Черновиков без согласования")
    assert widget.value == "1"
    assert widget.tone == "warning"

    action = next(a for a in out.actions if a.section == "accounting")
    assert action.count == 1
    assert action.href == "/accounting"


def test_accounting_widget_absent_without_module_access(db, make_user):
    user = make_user(admin=False)
    out = generate_today(db, user)
    assert not any(w.section == "accounting" for w in out.widgets)


def test_section_signal_reflects_real_data_and_is_cached(db, make_user):
    user = make_user(Module.ACCOUNTING, admin=True)
    employee_a = make_user()
    employee_b = make_user()

    empty = generate_section_signal(db, user, "accounting")
    assert empty.action is None

    create_money_movement(
        db,
        MoneyMovementCreate(subkind="salary_payout", amount=1000, employee_id=employee_a.id),
        initiator_id=user.id,
    )

    # Кеш ещё не тронут этим вызовом с force — свежий пересчёт видит черновик.
    fresh = generate_section_signal(db, user, "accounting", force=True)
    assert fresh.action is not None
    assert fresh.action.count == 1

    # Без force — отдаёт закешированный ответ (тот, что был до второй проводки).
    create_money_movement(
        db,
        MoneyMovementCreate(subkind="salary_payout", amount=2000, employee_id=employee_b.id),
        initiator_id=user.id,
    )
    cached = generate_section_signal(db, user, "accounting")
    assert cached.action.count == 1

    # force=True снова пересчитывает и обновляет кеш.
    updated = generate_section_signal(db, user, "accounting", force=True)
    assert updated.action.count == 2


def test_section_signal_hides_data_without_access(db, make_user):
    outsider = make_user(admin=False)
    out = generate_section_signal(db, outsider, "accounting")
    assert out.action is None


def test_section_signal_unknown_section_is_empty_not_error(db, make_user):
    user = make_user(admin=True)
    out = generate_section_signal(db, user, "meetings")
    assert out.section == "meetings"
    assert out.action is None


def test_section_signal_users_requires_admin_role(db, make_user):
    worker = make_user(admin=False)
    admin = make_user(admin=True)
    assert generate_section_signal(db, worker, "users").action is None
    # у свежесозданного admin нет workers без доступа выше нуля — просто не падает
    generate_section_signal(db, admin, "users")


def test_cache_hit_does_not_recompute_section_snapshot(db, make_user, monkeypatch):
    """0045: кэш должен экономить сам поход в builder, а не только сборку ответа —
    раньше _section_snapshot вызывался до проверки кэша и пересчитывал раздел
    заново на каждый запрос, кэш-хит или нет."""
    user = make_user(Module.ACCOUNTING, admin=True)
    generate_section_signal(db, user, "accounting", force=True)  # прогревает кэш реальным билдером

    def boom(_db):
        raise AssertionError("builder не должен вызываться при попадании в кэш")

    monkeypatch.setitem(SECTION_BUILDERS, "accounting", (Module.ACCOUNTING, boom))
    generate_section_signal(db, user, "accounting")  # кэш-хит — boom вызываться не должен


def test_warehouse_snapshot_avoids_per_material_query(db):
    """0045: _snapshot_warehouse раньше шёл через compute_breakdown на каждый
    материал (N+1) — теперь число запросов не должно расти с числом материалов."""
    for i in range(8):
        db.add(WarehouseMaterial(
            warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE,
            title=f"Материал {i}", code=f"M-{i}", unit="шт",
            quantity_in_stock=10, threshold=5,
        ))
    db.add(WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE,
        title="Дефицитный брус", code="M-shortage", unit="шт",
        quantity_in_stock=1, threshold=100,
    ))
    db.commit()

    queries: list[str] = []

    def track(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", track)
    try:
        snapshot = _snapshot_warehouse(db)
    finally:
        event.remove(engine, "before_cursor_execute", track)

    assert snapshot["total_materials"] == 9
    assert snapshot["materials_needing_supply"] == 1
    assert snapshot["top_shortage_materials"][0]["title"] == "Дефицитный брус"
    # было бы 9+ запросов при N+1 (по одному на материал) — фиксируем, что не растёт с N
    assert len(queries) <= 4


def test_section_signal_checked_only_for_sections_with_attention_entry(db, make_user):
    """0045: «checked» — правда только там, где сигнал реально проверяется по
    ATTENTION; для остального (например «cycle» — есть builder, но нет записи
    в ATTENTION) action=None не значит «всё хорошо», значит «не проверяли»."""
    user = make_user(Module.ACCOUNTING, Module.CYCLE, admin=True)

    checked_and_clean = generate_section_signal(db, user, "accounting")
    assert checked_and_clean.action is None
    assert checked_and_clean.checked is True

    has_builder_but_no_attention = generate_section_signal(db, user, "cycle")
    assert has_builder_but_no_attention.action is None
    assert has_builder_but_no_attention.checked is False

    unknown_section = generate_section_signal(db, user, "meetings")
    assert unknown_section.action is None
    assert unknown_section.checked is False

    no_access = generate_section_signal(db, make_user(admin=False), "accounting")
    assert no_access.action is None
    assert no_access.checked is False
