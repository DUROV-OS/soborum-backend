import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.clients.models import (
    CLIENT_STAGE_ORDER,
    MANUAL_TRANSITION_STAGES,
    Client,
    ClientChatLink,
    ClientNote,
    ClientStage,
    OrderType,
    PaymentPlan,
    stage_label,
)
from app.clients.schemas import (
    ClientBalancePaymentUpdate,
    ClientChatLinkCreate,
    ClientChatLinkUpdate,
    ClientCreate,
    ClientDocumentsUpdate,
    ClientHousesCountUpdate,
    ClientPaymentUpdate,
)
from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.tasks import service as task_service
from app.tasks import sync as task_sync
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.users import service as user_service

logger = logging.getLogger(__name__)


def _next_stage(stage: ClientStage) -> ClientStage | None:
    idx = CLIENT_STAGE_ORDER.index(stage)
    if idx + 1 < len(CLIENT_STAGE_ORDER):
        return CLIENT_STAGE_ORDER[idx + 1]
    return None


def needs_stage_task(stage: ClientStage) -> bool:
    """Нужна ли клиенту на этой стадии задача «перевести на следующую».

    Нужна только там, где переводит человек. Стадии после «Дом в
    производстве» двигает сама система по монтажу — см.
    `advance_stage_automatically`."""
    return stage in MANUAL_TRANSITION_STAGES


def _open_stage_tasks(db: Session, client_id: int) -> list[Task]:
    return (
        db.query(Task)
        .filter(
            Task.link_type == TaskLinkType.CLIENT_STAGE,
            Task.link_id == client_id,
            Task.status != TaskStatus.DONE,
        )
        .order_by(Task.id.desc())
        .all()
    )


def ensure_stage_transition_task(db: Session, client: Client) -> Task | None:
    """Гарантирует одну открытую задачу «перевести клиента на следующую стадию».

    Идемпотентна: если открытая задача под текущую стадию уже есть (или её
    стадия неизвестна — старые задачи без link_meta), ничего не создаёт.
    На стадиях, которые двигаются автоматически по монтажу («Дом в
    производстве», «Приёмка», «Успешно реализовано» — 0079), не создаёт
    ничего: переводить их руками некому и нечего. Вызывается при создании
    клиента, при смене стадии и фоновой сверкой (app/clients/reconcile.py).
    """
    if not needs_stage_task(client.stage):
        return None
    for task in _open_stage_tasks(db, client.id):
        meta_stage = (task.link_meta or {}).get("stage")
        if meta_stage in (None, client.stage.value):
            return task
    assignees = user_service.users_with_access(db, Module.CLIENTS)
    return task_service.create_link_task(
        db,
        title=f"Клиент «{client.full_name}»: перевести со стадии «{stage_label(client.stage)}» на следующую",
        link_type=TaskLinkType.CLIENT_STAGE,
        link_id=client.id,
        assignees=assignees,
        link_meta={"stage": client.stage.value},
    )


def create_client(db: Session, payload: ClientCreate) -> Client:
    cycle = Cycle(status=CycleStatus.CLIENT)
    db.add(cycle)
    db.flush()

    client = Client(
        cycle_id=cycle.id,
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        contacts=[c.model_dump() for c in payload.contacts],
    )
    db.add(client)
    db.flush()

    ensure_stage_transition_task(db, client)
    return client


def get_client_or_404(db: Session, client_id: int) -> Client:
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Клиент не найден")
    return client


def update_documents(db: Session, client: Client, payload: ClientDocumentsUpdate) -> Client:
    if client.documents_locked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Документные данные уже зафиксированы")
    data = payload.model_dump(exclude_unset=True)
    if data.get("advance_amount") is not None and data["advance_amount"] <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Аванс должен быть положительным")
    for field, value in data.items():
        setattr(client, field, value)
    db.flush()
    return client


def update_houses_count(db: Session, client: Client, payload: ClientHousesCountUpdate) -> Client:
    """В отличие от остального в `update_documents`, не проверяет
    `documents_locked_at` — количество домов редактируется в любой момент
    (0044). Изменение после того, как на «Постоплате» уже заведены проекты
    производства по старому количеству, задним числом их не пересчитывает —
    это ручная правка данных, не автоматика."""
    count = payload.houses_count
    if count < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Количество домов не может быть меньше 1")
    if client.order_type != OrderType.MULTIPLE and count != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Количество домов > 1 доступно только для множественного заказа",
        )
    client.houses_count = count
    db.flush()
    return client


def _sale_income_amount_on_is_paid(client: Client) -> float | None:
    """Сумма «дохода от продажи» на переходе `is_paid` → `True` — зависит от
    формата расчёта (0011-f): `POST_PAYMENT` на этом шаге денег не даёт."""
    if client.payment_plan == PaymentPlan.FULL_PREPAYMENT:
        return client.final_price
    if client.payment_plan == PaymentPlan.ADVANCE_THEN_BALANCE:
        return client.advance_amount
    return None


def _sale_income_amount_on_balance_paid(client: Client) -> float | None:
    """Сумма «дохода от продажи» на переходе `balance_paid` → `True`.
    `FULL_PREPAYMENT` сюда не доходит — этот шаг для неё недоступен."""
    if client.payment_plan == PaymentPlan.ADVANCE_THEN_BALANCE:
        if client.final_price is None or client.advance_amount is None:
            return None
        return client.final_price - client.advance_amount
    if client.payment_plan == PaymentPlan.POST_PAYMENT:
        return client.final_price
    return None


def _record_sale_income(db: Session, client: Client, amount: float | None, initiator_id: int | None) -> None:
    """Побочный эффект оплаты клиента — создаёт проводку «доход от продажи» в
    «Бухгалтерии». Ошибка здесь не должна ронять основной флоу оплаты клиента
    (0011-f) — логируем и продолжаем."""
    if not amount or amount <= 0 or initiator_id is None:
        return
    from app.accounting import service as accounting_service

    try:
        accounting_service.record_sale_income(
            db, client_id=client.id, amount=amount, initiator_id=initiator_id
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось создать проводку «доход от продажи» для клиента %s", client.id)


def set_payment_edit_unlocked(db: Session, client: Client, unlocked: bool) -> Client:
    """Разрешение администратора обходить `payment_locked_at` (0054). Не
    трогает сам факт блокировки — только снимает запрет на редактирование,
    пока включено."""
    client.payment_edit_unlocked = unlocked
    db.flush()
    return client


def update_payment(
    db: Session, client: Client, payload: ClientPaymentUpdate, initiator_id: int | None = None
) -> Client:
    if client.payment_locked_at is not None and not client.payment_edit_unlocked:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Статус оплаты уже зафиксирован")
    was_paid = client.is_paid
    client.is_paid = payload.is_paid
    db.flush()
    if payload.is_paid and not was_paid:
        _record_sale_income(db, client, _sale_income_amount_on_is_paid(client), initiator_id)
    return client


def record_balance_payment(
    db: Session,
    client: Client,
    payload: ClientBalancePaymentUpdate,
    initiator_id: int | None = None,
) -> Client:
    """Отметить приём остатка «после получения». Осмысленно только после
    старта производства (стадии «Дом в производстве» и «Приёмка») и только для
    планов с оплатой после получения дома — у полной предоплаты остаток
    погашен ещё на стадии «Договор подписан/Аванс внесён». На «Успешно
    реализовано» цикл уже закрыт, а закрыть его с непогашенным остатком
    нельзя (app.installation.service.complete_installation)."""
    if client.payment_plan == PaymentPlan.FULL_PREPAYMENT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="У клиента полная предоплата — остаток «после получения» не предусмотрен",
        )
    if client.stage not in (ClientStage.POSTPAYMENT, ClientStage.ACCEPTANCE):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Остаток «после получения» принимается на стадиях «Дом в производстве» и «Приёмка»",
        )
    was_paid = client.balance_paid
    client.balance_paid = payload.balance_paid
    client.balance_paid_at = datetime.now(timezone.utc) if payload.balance_paid else None
    db.flush()
    if payload.balance_paid:
        task_service.close_open_link_task(db, TaskLinkType.CLIENT_BALANCE_PAYMENT, client.id)
        if not was_paid:
            _record_sale_income(db, client, _sale_income_amount_on_balance_paid(client), initiator_id)
    return client


def _set_document_file(db: Session, client: Client, field: str, file_id: int) -> Client:
    if client.documents_locked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Документные данные уже зафиксированы")
    setattr(client, field, file_id)
    db.flush()
    return client


def set_contract_files(db: Session, client: Client, contract_file_id: int, appendix_file_id: int) -> Client:
    """Обычный путь загрузки (0061): договор и приложение к договору одним
    действием на фронте — нет эндпоинта на один без другого."""
    if client.documents_locked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Документные данные уже зафиксированы")
    client.contract_file_id = contract_file_id
    client.contract_appendix_file_id = appendix_file_id
    db.flush()
    return client


def set_contract_file(db: Session, client: Client, file_id: int) -> Client:
    """Точечная установка одного файла — для сценариев вроде генерации
    документа Мариной по одному, где второй документ приходит отдельным
    вызовом. Гейт стадии всё равно требует оба (contract_file_id и
    contract_appendix_file_id) — см. _DOCUMENTS_REQUIRED."""
    return _set_document_file(db, client, "contract_file_id", file_id)


def set_contract_appendix_file(db: Session, client: Client, file_id: int) -> Client:
    return _set_document_file(db, client, "contract_appendix_file_id", file_id)


def set_house_project_file(db: Session, client: Client, file_id: int) -> Client:
    return _set_document_file(db, client, "house_project_file_id", file_id)


def set_ar_file(db: Session, client: Client, file_id: int) -> Client:
    return _set_document_file(db, client, "ar_file_id", file_id)


def set_kr_file(db: Session, client: Client, file_id: int) -> Client:
    return _set_document_file(db, client, "kr_file_id", file_id)


def create_chat_link(db: Session, client: Client, payload: ClientChatLinkCreate) -> ClientChatLink:
    """Привязать ещё один чат MAX к клиенту (0053) — один клиент может иметь
    несколько чатов (например, отдельно с ним и с его помощником), но каждый
    чат по-прежнему принадлежит не более чем одному клиенту."""
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название привязки обязательно")
    taken = db.query(ClientChatLink).filter(ClientChatLink.max_chat_id == payload.max_chat_id).first()
    if taken:
        if taken.client_id == client.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Этот чат уже привязан к этому клиенту"
            )
        taken_client = db.get(Client, taken.client_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Чат уже привязан к клиенту «{taken_client.full_name}» — сначала открепите его там",
        )
    link = ClientChatLink(client_id=client.id, max_chat_id=payload.max_chat_id, label=label)
    db.add(link)
    db.flush()
    return link


def get_chat_link_or_404(db: Session, client_id: int, link_id: int) -> ClientChatLink:
    link = db.get(ClientChatLink, link_id)
    if not link or link.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Привязка не найдена")
    return link


def update_chat_link(db: Session, link: ClientChatLink, payload: ClientChatLinkUpdate) -> ClientChatLink:
    data = payload.model_dump(exclude_unset=True)
    if "label" in data:
        label = (data["label"] or "").strip()
        if not label:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название привязки обязательно")
        link.label = label
    if "state" in data:
        link.state = data["state"]
    db.flush()
    return link


def delete_chat_link(db: Session, link: ClientChatLink) -> None:
    db.delete(link)
    db.flush()


def add_note(db: Session, client: Client, author_id: int, text: str) -> ClientNote:
    note = ClientNote(client_id=client.id, author_id=author_id, text=text)
    db.add(note)
    db.flush()
    return note


def update_note(db: Session, note: ClientNote, text: str) -> ClientNote:
    note.text = text
    db.flush()
    return note


def delete_note(db: Session, note: ClientNote) -> None:
    db.delete(note)
    db.flush()


def delete_client(db: Session, client: Client) -> None:
    """Удалить клиента и его заметки (каскад на уровне БД). Отказ 409, если
    цикл ещё не завершён, есть незакрытые задачи по клиенту или проводки,
    ссылающиеся на него — по умолчанию запрет, а не тихий каскад (см. спеку
    0030-a). Ни цикл, ни производство/монтаж под ним не трогаем — они остаются
    как историческая запись."""
    if client.cycle.status != CycleStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить клиента: цикл ещё не завершён",
        )
    open_task = (
        db.query(Task)
        .filter(
            Task.link_type.in_([TaskLinkType.CLIENT_STAGE, TaskLinkType.CLIENT_BALANCE_PAYMENT]),
            Task.link_id == client.id,
            Task.status != TaskStatus.DONE,
        )
        .first()
    )
    if open_task is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить клиента: есть незавершённые задачи",
        )
    from app.accounting.models import MoneyMovement

    has_money_movements = (
        db.query(MoneyMovement.id).filter(MoneyMovement.client_id == client.id).first() is not None
    )
    if has_money_movements:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить клиента: есть проводки по клиенту",
        )
    db.delete(client)
    db.flush()


_DOCUMENTS_REQUIRED = [
    "order_type",
    "final_price",
    "installation_address",
    "contract_file_id",
    "contract_appendix_file_id",
    "ar_file_id",
    "kr_file_id",
]
# house_project_file_id сознательно не в списке — с 0061 необязателен: не у
# каждого клиента он есть в системе.


def transition_stage(db: Session, client: Client) -> Client:
    if client.stage == ClientStage.COMPLETED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Клиент уже на последней стадии")
    if not needs_stage_task(client.stage):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Стадия «{stage_label(client.stage)}» двигается автоматически "
                "по разделу «Монтаж» — вручную её не переводят"
            ),
        )
    next_stage = _next_stage(client.stage)

    # DISCUSSION requires nothing (0044 removed the wishes/area/price/layout
    # "project" group that used to be filled and locked here) — the stage
    # still exists in the pipeline, it just no longer gates anything.

    if client.stage == ClientStage.APPROVAL:
        missing = [f for f in _DOCUMENTS_REQUIRED if getattr(client, f) is None]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Не заполнены документные поля: {', '.join(missing)}",
            )
        if client.order_type == OrderType.SINGLE:
            client.houses_count = 1
        elif client.order_type == OrderType.MULTIPLE and client.houses_count < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Для множественного заказа укажите количество домов (не меньше 2)",
            )
        if client.payment_plan == PaymentPlan.ADVANCE_THEN_BALANCE:
            if client.advance_amount is None or client.advance_amount <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Для плана «аванс + оплата после получения» укажите сумму аванса",
                )
            if client.final_price is not None and client.advance_amount >= client.final_price:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Аванс должен быть меньше итоговой стоимости",
                )
        client.documents_locked_at = datetime.now(timezone.utc)

    elif client.stage == ClientStage.PAYMENT:
        # В какой момент нужны деньги — зависит от плана оплаты.
        if client.payment_plan == PaymentPlan.POST_PAYMENT:
            # Деньги на этой стадии не требуются, производство стартует сразу.
            if client.is_paid is None:
                client.is_paid = False
        else:
            # Полная предоплата — вся сумма; аванс + остаток — аванс.
            if client.is_paid is not True:
                detail = (
                    "Не подтверждено поступление полной предоплаты"
                    if client.payment_plan == PaymentPlan.FULL_PREPAYMENT
                    else "Не подтверждено поступление аванса"
                )
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
        if client.payment_plan == PaymentPlan.FULL_PREPAYMENT:
            # Остаток «после получения» уже покрыт полной предоплатой.
            client.balance_paid = True
            client.balance_paid_at = datetime.now(timezone.utc)
        client.payment_locked_at = datetime.now(timezone.utc)

    client.stage = next_stage
    db.flush()

    if next_stage == ClientStage.POSTPAYMENT:
        from app.production.models import Production

        houses = client.houses_count if client.order_type == OrderType.MULTIPLE else 1
        new_productions = []
        for i in range(1, houses + 1):
            production = Production(
                cycle_id=client.cycle_id,
                house_index=i,
                name=f"Дом {i}" if houses > 1 else "Дом",
            )
            db.add(production)
            new_productions.append(production)
        client.cycle.status = CycleStatus.PRODUCTION
        db.flush()

        # ИИ строит (или переиспользует) граф этапов производства по КР и,
        # если шаблон уже подтверждён, сразу применяет его — см. 0066-f.
        # Один вызов транзакции = одна генерация даже на мультидом: второй и
        # третий дом этого же клиента переиспользуют template_cache, не
        # только house_model_key-кеш самой generate_or_reuse_template (тот
        # работает МЕЖДУ клиентами одной модели, этот — ВНУТРИ одного перехода
        # для мультидома, в т.ч. индивидуальных проектов без house_model_key).
        template_cache: dict[str, object] = {}
        for production in new_productions:
            _apply_stage_plan(db, client, production, template_cache)

        if client.payment_plan != PaymentPlan.FULL_PREPAYMENT:
            _create_balance_payment_task(db, client)

    _reset_stage_tasks(db, client)
    return client


def _reset_stage_tasks(db: Session, client: Client) -> None:
    """Закрывает все открытые задачи прошлой стадии (обычно одна; сверка могла
    оставить дубликат) и заводит одну под новую стадию."""
    for task in _open_stage_tasks(db, client.id):
        task_service.force_close(db, task)
    ensure_stage_transition_task(db, client)


def advance_stage_automatically(db: Session, client: Client | None, target: ClientStage) -> Client | None:
    """Двигает клиента на стадию, которой управляет не человек, а ход работ
    («Приёмка» по выходу монтажа на проработку, «Успешно реализовано» по
    завершению монтажа — 0079).

    Только вперёд: клиента, уже стоящего на `target` или дальше, не трогает,
    поэтому повторный вызов из монтажа ничего не ломает. Задачи стадии
    пересобираются так же, как при ручном переходе.
    """
    if client is None:
        return None
    if CLIENT_STAGE_ORDER.index(client.stage) >= CLIENT_STAGE_ORDER.index(target):
        return client
    client.stage = target
    db.flush()
    _reset_stage_tasks(db, client)
    return client


def _apply_stage_plan(db: Session, client: Client, production, template_cache: dict[str, object]) -> None:
    """Строит/переиспользует граф этапов производства по КР ([[0066-d]]) и,
    если он уже подтверждён, сразу применяет к `production` ([[0066-f]]).

    Намеренно не роняет переход клиента по стадиям: нет разбора КР, нет ключа
    ИИ, сбой сети/генерации — производство просто остаётся без блоков до
    ручного запуска (тот же принцип деградации, что у `production/deadlines.py`
    — переход клиента со стадии на стадию не должен зависеть от готовности
    ИИ-инфраструктуры)."""
    from app.production.stage_plan import instantiate_stage_plan
    from app.production.stage_template_service import generate_or_reuse_template
    from app.production.stage_templates import TemplateStatus

    template = template_cache.get("template")
    if template is None:
        try:
            template = generate_or_reuse_template(db, client)
        except HTTPException as error:
            logger.warning(
                "Автогенерация шаблона графа этапов для клиента %s пропущена: %s", client.id, error.detail
            )
            return
        template_cache["template"] = template

    if template.status == TemplateStatus.CONFIRMED:  # type: ignore[union-attr]
        instantiate_stage_plan(db, production, template)


def _create_balance_payment_task(db: Session, client: Client) -> None:
    assignees = user_service.users_with_access(db, Module.CLIENTS)
    task_service.create_link_task(
        db,
        title=f"Клиент «{client.full_name}»: принять оплату после получения (остаток)",
        link_type=TaskLinkType.CLIENT_BALANCE_PAYMENT,
        link_id=client.id,
        assignees=assignees,
    )


@task_sync.register(TaskLinkType.CLIENT_BALANCE_PAYMENT)
def _on_balance_payment_task_closed(db: Session, task) -> None:
    client = db.get(Client, task.link_id)
    if client is not None and not client.balance_paid:
        record_balance_payment(db, client, ClientBalancePaymentUpdate(balance_paid=True))


@task_sync.register(TaskLinkType.CLIENT_STAGE)
def _on_client_task_closed(db: Session, task) -> None:
    client = db.get(Client, task.link_id)
    if client is not None:
        transition_stage(db, client)
