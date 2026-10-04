"""Текст задачи «перевести клиента на следующую стадию» (0094):

- заголовок с человеческой подписью стадии, без значения enum;
- описание говорит, куда переводить, где кнопка и что проверит система —
  на «Лиде» ничего, на «Ипотеке/Одобрении» документы, на «Договор подписан»
  оплату;
- сверка переписывает текст старых задач (заголовок с «approval», пустое
  описание), но не трогает задачи без стадии в link_meta;
- ошибка перехода с «Одобрения» перечисляет поля подписями карточки.
"""

import pytest
from fastapi import HTTPException

from app.clients import service as client_service
from app.clients.models import ClientStage
from app.clients.reconcile import reconcile_client_stage_tasks
from app.clients.schemas import ClientCreate
from app.tasks import service as task_service
from app.tasks.models import Task, TaskLinkType, TaskStatus


def _make_client(db, name="Клиент"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def _open_stage_task(db, client_id):
    tasks = (
        db.query(Task)
        .filter(
            Task.link_type == TaskLinkType.CLIENT_STAGE,
            Task.link_id == client_id,
            Task.status != TaskStatus.DONE,
        )
        .all()
    )
    assert len(tasks) == 1
    return tasks[0]


def test_new_client_task_explains_what_to_do(db):
    client = _make_client(db, "Новый")
    task = _open_stage_task(db, client.id)

    assert task.title == "Клиент «Новый»: перевести со стадии «Лид» на следующую"
    assert "Перевести на «Обсуждение»" in task.description
    assert "ничего не нужно" in task.description
    assert "закроется сама" in task.description


def test_approval_task_lists_gate_requirements_with_human_labels(db):
    client = _make_client(db, "Одобрение")
    for _ in range(3):  # LEAD -> DISCUSSION -> SITE_VISIT -> APPROVAL
        client_service.transition_stage(db, client)
    task = _open_stage_task(db, client.id)

    assert "«Ипотека/Одобрение в банке»" in task.title
    assert "approval" not in task.title
    assert "Перевести на «Договор подписан/Аванс внесён»" in task.description
    for label in ("Итоговая цена", "Адрес установки", "АР", "КР", "приложение к договору"):
        assert label in task.description


def test_reconcile_rewrites_legacy_task_text(db):
    client = _make_client(db, "Сиверкина")
    for _ in range(3):
        client_service.transition_stage(db, client)
    task = _open_stage_task(db, client.id)
    # так выглядит задача, заведённая до человеческих подписей стадий
    task.title = "Клиент «Сиверкина»: перевести со стадии «approval» на следующую"
    task.description = None
    db.flush()

    report = reconcile_client_stage_tasks(db)

    assert report["retexted"] == 1
    assert report["created"] == 0
    assert task.title == "Клиент «Сиверкина»: перевести со стадии «Ипотека/Одобрение в банке» на следующую"
    assert task.description == client_service.stage_task_description(client)
    assert reconcile_client_stage_tasks(db)["retexted"] == 0


def test_reconcile_leaves_task_without_stage_meta_alone(db):
    client = _make_client(db, "Безмета")
    task_service.force_close(db, _open_stage_task(db, client.id))
    legacy = task_service.create_link_task(
        db,
        title="старый текст",
        link_type=TaskLinkType.CLIENT_STAGE,
        link_id=client.id,
        assignees=[],
    )

    report = reconcile_client_stage_tasks(db)

    assert report["retexted"] == 0
    assert legacy.title == "старый текст"


def test_approval_gate_error_uses_card_labels(db):
    client = _make_client(db, "Пустой")
    for _ in range(3):
        client_service.transition_stage(db, client)

    with pytest.raises(HTTPException) as exc:
        client_service.transition_stage(db, client)

    detail = exc.value.detail
    assert detail.startswith("Не заполнены поля в карточке клиента:")
    assert "Итоговая цена" in detail and "Приложение к договору" in detail
    assert "final_price" not in detail and "_file_id" not in detail
    assert client.stage == ClientStage.APPROVAL
