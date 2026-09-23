"""Раскатка уровней доступа на остальные разделы бэка (0052-b): для каждого
из production, installation, cycle, warehouse, marketing, house_models,
tasks, ai, board, accounting — `view` отклоняет запись, `edit` отклоняет
разрушительные/административные действия раздела (там, где они есть),
`full`/`ADMIN` проходит везде. Разрушительные действия production/warehouse
(удаление, списание) уже покрыты отдельно в `test_production_delete.py` /
`test_warehouse_write_off.py` / `test_warehouse_supplier_delete.py` —
здесь не дублируются.
"""

from app.board.models import BoardNode
from app.common.module_access import AccessLevel, Module
from app.cycle.models import Cycle, CycleStatus
from app.installation.models import Installation, InstallationStage


# --------------------------------------------------------------- installation --

def _make_installation(db):
    cycle = Cycle(status=CycleStatus.INSTALLATION)
    db.add(cycle)
    db.flush()
    installation = Installation(cycle_id=cycle.id, stage=InstallationStage.DELIVERY)
    db.add(installation)
    db.flush()
    return installation


def test_installation_view_allows_read_rejects_write(api, make_user, db):
    installation = _make_installation(db)
    db.commit()
    viewer = api(make_user(Module.INSTALLATION, level=AccessLevel.VIEW))

    assert viewer.get(f"/api/installation/{installation.id}").status_code == 200
    resp = viewer.patch(f"/api/installation/{installation.id}", json={"notes": "не должно пройти"})
    assert resp.status_code == 403


def test_installation_edit_allows_write(api, make_user, db):
    installation = _make_installation(db)
    db.commit()
    editor = api(make_user(Module.INSTALLATION, level=AccessLevel.EDIT))

    resp = editor.patch(f"/api/installation/{installation.id}", json={"notes": "проработка идёт"})
    assert resp.status_code == 200
    assert resp.json()["notes"] == "проработка идёт"


def test_installation_none_rejects_read(api, make_user, db):
    installation = _make_installation(db)
    db.commit()
    outsider = api(make_user())
    assert outsider.get(f"/api/installation/{installation.id}").status_code == 403


# ------------------------------------------------------------------------ cycle --

def test_cycle_view_allows_read(api, make_user, db):
    cycle = Cycle(status=CycleStatus.CLIENT)
    db.add(cycle)
    db.commit()
    viewer = api(make_user(Module.CYCLE, level=AccessLevel.VIEW))

    assert viewer.get("/api/cycles/").status_code == 200
    assert viewer.get(f"/api/cycles/{cycle.id}").status_code == 200


def test_cycle_none_rejects_read(api, make_user, db):
    outsider = api(make_user())
    assert outsider.get("/api/cycles/").status_code == 403


# -------------------------------------------------------------------- marketing --

def test_marketing_view_allows_read_rejects_write(api, make_user, db):
    from app.marketing import service as marketing_service
    from app.marketing.schemas import ContentItemCreate

    content = marketing_service.create_content(db, ContentItemCreate(title="Пост"))
    db.commit()
    viewer = api(make_user(Module.MARKETING, level=AccessLevel.VIEW))

    assert viewer.get("/api/marketing/calendar").status_code == 200
    resp = viewer.patch(f"/api/marketing/content/{content.id}", json={"title": "Новый заголовок"})
    assert resp.status_code == 403


def test_marketing_edit_allows_write(api, make_user, db):
    editor = api(make_user(Module.MARKETING, level=AccessLevel.EDIT))
    resp = editor.post("/api/marketing/content", json={"title": "Пост про дома"})
    assert resp.status_code == 201, resp.text


def test_marketing_none_rejects_read(api, make_user, db):
    outsider = api(make_user())
    assert outsider.get("/api/marketing/calendar").status_code == 403


# ------------------------------------------------------------------ house_models --

def test_house_models_view_allows_read(api, make_user, db):
    from app.house_models.import_kb import ensure_house_models_seed

    ensure_house_models_seed(db)
    db.commit()
    viewer = api(make_user(Module.HOUSE_MODELS, level=AccessLevel.VIEW))
    assert viewer.get("/api/house-models/catalog").status_code == 200


def test_house_models_none_rejects_read(api, make_user, db):
    outsider = api(make_user())
    assert outsider.get("/api/house-models/catalog").status_code == 403


# ----------------------------------------------------------------------- tasks --

def test_tasks_view_allows_read_rejects_write(api, make_user, db):
    viewer_user = make_user(Module.TASKS, level=AccessLevel.VIEW)
    viewer = api(viewer_user)

    assert viewer.get("/api/tasks/").status_code == 200
    resp = viewer.post("/api/tasks/", json={"title": "Новая задача"})
    assert resp.status_code == 403


def test_tasks_edit_allows_write(api, make_user, db):
    editor = api(make_user(Module.TASKS, level=AccessLevel.EDIT))
    resp = editor.post("/api/tasks/", json={"title": "Новая задача"})
    assert resp.status_code == 201, resp.text


def test_tasks_none_rejects_read(api, make_user, db):
    outsider = api(make_user())
    assert outsider.get("/api/tasks/").status_code == 403


# ----------------------------------------------------------------------- board --

def _make_board_root(db):
    root = BoardNode(parent_id=None, level=0, title="Компания", description="")
    db.add(root)
    db.flush()
    return root


def test_board_view_allows_read_rejects_write(api, make_user, db):
    root = _make_board_root(db)
    db.commit()
    viewer = api(make_user(Module.BOARD, level=AccessLevel.VIEW))

    assert viewer.get("/api/board/tree").status_code == 200
    resp = viewer.patch(f"/api/board/nodes/{root.id}", json={"title": "Не должно пройти"})
    assert resp.status_code == 403


def test_board_edit_allows_write_rejects_actualize(api, make_user, db):
    root = _make_board_root(db)
    db.commit()
    editor = api(make_user(Module.BOARD, level=AccessLevel.EDIT))

    resp = editor.patch(f"/api/board/nodes/{root.id}", json={"title": "Обновлённое название"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Обновлённое название"

    assert editor.post("/api/board/actualize").status_code == 403


def test_board_full_level_passes_actualize_permission_gate(api, make_user, db):
    _make_board_root(db)
    db.commit()
    full_worker = api(make_user(Module.BOARD, level=AccessLevel.FULL))
    # Без ANTHROPIC_API_KEY (тестовое окружение) дальше 400, но не 403 —
    # это и означает, что require_full пропустил не-ADMIN сотрудника.
    resp = full_worker.post("/api/board/actualize")
    assert resp.status_code != 403


def test_board_none_rejects_read(api, make_user, db):
    outsider = api(make_user())
    assert outsider.get("/api/board/tree").status_code == 403


# ------------------------------------------------------------------ accounting --


def _default_account_id(api_client) -> int:
    """Счёт по умолчанию (0081-a): с этой задачи проводка без счёта — 422."""
    accounts = api_client.get("/api/accounting/accounts").json()
    return next(a["id"] for a in accounts if a["is_default"])


def test_accounting_view_allows_read_rejects_write(api, make_user, db):
    viewer = api(make_user(Module.ACCOUNTING, level=AccessLevel.VIEW))

    assert viewer.get("/api/accounting/money-movements").status_code == 200
    resp = viewer.post(
        "/api/accounting/money-movements", json={"subkind": "other_income", "amount": 1000, "tax": 0}
    )
    assert resp.status_code == 403


def test_accounting_edit_allows_write_rejects_delete(api, make_user, db):
    editor = api(make_user(Module.ACCOUNTING, level=AccessLevel.EDIT))
    resp = editor.post(
        "/api/accounting/money-movements",
        json={
            "subkind": "other_income",
            "amount": 1000,
            "tax": 0,
            "account_id": _default_account_id(editor),
        },
    )
    assert resp.status_code == 201, resp.text
    mm_id = resp.json()["id"]

    assert editor.delete(f"/api/accounting/money-movements/{mm_id}").status_code == 403


def test_accounting_full_level_allows_delete_without_admin_role(api, make_user, db):
    full_worker = api(make_user(Module.ACCOUNTING, level=AccessLevel.FULL))
    mm_id = full_worker.post(
        "/api/accounting/money-movements",
        json={
            "subkind": "other_income",
            "amount": 1000,
            "tax": 0,
            "account_id": _default_account_id(full_worker),
        },
    ).json()["id"]

    assert full_worker.delete(f"/api/accounting/money-movements/{mm_id}").status_code == 204


def test_accounting_none_rejects_read(api, make_user, db):
    outsider = api(make_user())
    assert outsider.get("/api/accounting/money-movements").status_code == 403


# ------------------------------------------------------------------------- ai --

def test_ai_ask_rejects_without_section_access(api, make_user, db):
    """AI=edit, но нет доступа к CLIENTS — 403 до обращения к Марине."""
    user = api(make_user(Module.AI, level=AccessLevel.EDIT))
    resp = user.post("/api/ai/clients/ask", json={"message": "Привет"})
    assert resp.status_code == 403


def test_ai_ask_rejects_without_ai_access(api, make_user, db):
    """CLIENTS=view, но нет доступа к AI — 403 до обращения к Марине."""
    user = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    resp = user.post("/api/ai/clients/ask", json={"message": "Привет"})
    assert resp.status_code == 403


def _make_multi_grant_user(db, grants: dict[Module, AccessLevel]):
    from app.users.models import User, UserModuleAccess, UserRole

    user = User(
        email=f"multi{db.query(User).count()}@example.com",
        full_name="Тестовый сотрудник",
        hashed_password="not-a-login-password",
        is_active=True,
        role=UserRole.WORKER,
    )
    user.module_access = [UserModuleAccess(module=m, level=lvl) for m, lvl in grants.items()]
    db.add(user)
    db.commit()
    return user


def test_ai_ask_passes_permission_gate_with_both_grants(api, db):
    user = _make_multi_grant_user(db, {Module.AI: AccessLevel.EDIT, Module.CLIENTS: AccessLevel.VIEW})
    resp = api(user).post("/api/ai/clients/ask", json={"message": "Привет"})
    # Без ANTHROPIC_API_KEY дальше 503 ("Марина не подключена"), но не 403.
    assert resp.status_code != 403


def test_ai_analytics_requires_only_view_on_ai(api, db):
    """GET .../analytics — читающая операция, поэтому на AI достаточно view."""
    user = _make_multi_grant_user(db, {Module.AI: AccessLevel.VIEW, Module.CLIENTS: AccessLevel.VIEW})
    resp = api(user).get("/api/ai/clients/analytics")
    assert resp.status_code != 403


def test_ai_mcp_status_requires_full(api, make_user, db):
    editor = api(make_user(Module.AI, level=AccessLevel.EDIT))
    assert editor.get("/api/ai/mcp/status").status_code == 403

    full_worker = api(make_user(Module.AI, level=AccessLevel.FULL))
    resp = full_worker.get("/api/ai/mcp/status")
    assert resp.status_code == 200


def test_ai_none_rejects_chat_list(api, make_user, db):
    outsider = api(make_user())
    assert outsider.get("/api/ai/chats").status_code == 403


# ------------------------------------------------------------------ ADMIN sweep --

def test_admin_passes_write_and_destructive_in_every_rolled_out_section(api, make_user, db):
    admin = api(make_user(admin=True))
    installation = _make_installation(db)
    root = _make_board_root(db)
    db.commit()

    assert admin.patch(f"/api/installation/{installation.id}", json={"notes": "ок"}).status_code == 200
    assert admin.patch(f"/api/board/nodes/{root.id}", json={"title": "Совет"}).status_code == 200
    assert admin.post("/api/tasks/", json={"title": "Задача админа"}).status_code == 201
    assert admin.post("/api/marketing/content", json={"title": "Пост админа"}).status_code == 201
    mm_id = admin.post(
        "/api/accounting/money-movements",
        json={
            "subkind": "other_income",
            "amount": 500,
            "tax": 0,
            "account_id": _default_account_id(admin),
        },
    ).json()["id"]
    assert admin.delete(f"/api/accounting/money-movements/{mm_id}").status_code == 204
    assert admin.get("/api/ai/mcp/status").status_code == 200
