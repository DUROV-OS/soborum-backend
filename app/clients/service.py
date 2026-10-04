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
    ContractSource,
    OrderType,
    PaymentPlan,
    balance_due_deadline,
    stage_label,
)
from app.clients.schemas import (
    ClientBalanceDueDateUpdate,
    ClientBalancePaymentUpdate,
    ClientChatLinkCreate,
    ClientChatLinkUpdate,
    ClientContractVerify,
    ClientCreate,
    ClientDocumentsUpdate,
    ClientHousesCountUpdate,
    ClientPaymentUpdate,
    ClientSourceUpdate,
    ClientTaskClose,
    ClientTaskCreate,
    ClientTaskDeadlineUpdate,
)
from app.common.module_access import AccessLevel, Module
from app.cycle.models import Cycle, CycleStatus
from app.partners.models import Partner
from app.tasks import service as task_service
from app.tasks import sync as task_sync
from app.tasks.models import Task, TaskLinkType, TaskReportKind, TaskStatus
from app.users import service as user_service
from app.users.models import User

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


# Что проверяет `transition_stage` при уходе с каждой ручной стадии — для
# описания задачи «перевести на следующую». Держать в согласии с гейтами ниже.
_STAGE_TASK_REQUIREMENTS: dict[ClientStage, list[str]] = {
    ClientStage.APPROVAL: [
        "заполнить в карточке «Проект», «Итоговая цена», «Адрес установки» и «Формат расчёта» "
        "(для «аванс + оплата после получения» — ещё «Сумма аванса», меньше итоговой цены)",
        "загрузить АР, КР, договор и приложение к договору и отметить, что договор и приложение проверены",
        "для множественного заказа — указать количество домов (не меньше 2)",
    ],
    ClientStage.PAYMENT: [
        "подтвердить в карточке поступление полной предоплаты или аванса — смотря по формату расчёта "
        "(при «оплате после получения» подтверждать ничего не нужно)",
    ],
}


def stage_task_title(client: Client) -> str:
    return f"Клиент «{client.full_name}»: перевести со стадии «{stage_label(client.stage)}» на следующую"


def stage_task_description(client: Client) -> str:
    """Что сделать по задаче стадии: куда переводить, где кнопка и что
    система потребует. Без этого задача читается как «реши что-то» (0094)."""
    next_stage = _next_stage(client.stage)
    next_label = stage_label(next_stage) if next_stage else ""
    lines = [
        f"Когда клиент прошёл стадию «{stage_label(client.stage)}», переведите его на «{next_label}»: "
        f"откройте карточку клиента и нажмите «Перевести на «{next_label}»».",
    ]
    requirements = _STAGE_TASK_REQUIREMENTS.get(client.stage)
    if requirements:
        lines.append("Перед переводом система проверит:")
        lines.extend(f"— {item};" for item in requirements)
    else:
        lines.append("Заполнять для перевода ничего не нужно — система его не проверяет.")
    lines.append("Перевести не получится, пока у клиента открыта блокирующая задача.")
    lines.append("Эта задача закроется сама после перевода клиента.")
    return "\n".join(lines)


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
        title=stage_task_title(client),
        description=stage_task_description(client),
        link_type=TaskLinkType.CLIENT_STAGE,
        link_id=client.id,
        assignees=assignees,
        link_meta={"stage": client.stage.value},
    )


def _clean_source(db: Session, payload: ClientSourceUpdate) -> dict:
    """Приводит источник клиента к одному из двух валидных состояний: «привело
    агентство, известно какое» или «пришёл сам, полей агентства нет». Плюс
    рекомендатель из базы партнёров (0083-c) — он независим от галочки
    агентства: у клиента может быть и то и другое, и ни того ни другого."""
    referrer_id = payload.referrer_partner_id
    if referrer_id is not None and db.get(Partner, referrer_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Партнёр, указанный как рекомендатель, не найден в базе партнёров",
        )
    return {**_clean_agency(payload), "referrer_partner_id": referrer_id}


def _clean_agency(payload: ClientSourceUpdate) -> dict:
    name = (payload.agency_name or "").strip()
    contact = (payload.agency_contact or "").strip()
    if not payload.via_agency:
        return {"via_agency": False, "agency_name": None, "agency_contact": None}
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Укажите, какое агентство привело клиента",
        )
    return {"via_agency": True, "agency_name": name, "agency_contact": contact or None}


def update_source(db: Session, client: Client, payload: ClientSourceUpdate) -> Client:
    """Источник, в отличие от ФИО/телефона/почты, не замораживается после
    создания: то, что клиента привело агентство, нередко выясняется позже."""
    for field, value in _clean_source(db, payload).items():
        setattr(client, field, value)
    db.flush()
    return client


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
        **_clean_source(db, payload),
    )
    db.add(client)
    db.flush()

    ensure_stage_transition_task(db, client)
    return client


def search_clients(clients: list[Client], search: str | None) -> list[Client]:
    """Отбор клиентов по фамилии/имени и телефону (0079-f).

    По ФИО — вхождение без учёта регистра в любую часть строки, так что
    работает и по фамилии, и по имени. По телефону — по цифрам: в базе он
    лежит как его записал менеджер (`+7 900 123-45-67`), а ищут и
    `89001234567`, и последние цифры. Ведущая 8/7 отбрасывается с обеих
    сторон, чтобы записи одного российского номера сходились.

    Отбор идёт в Python, а не в SQL, осознанно: список клиентов эндпоинт и
    так отдаёт целиком, а регистронезависимое сравнение кириллицы в SQL
    ведёт себя по-разному в PostgreSQL и SQLite (на котором гоняются тесты)
    — поведение разъехалось бы между продом и проверками.

    Почта, адрес и заметки сознательно не ищутся — заказчик просил искать по
    фамилии или телефону.
    """
    text = (search or "").strip()
    if not text:
        return clients

    lowered = text.lower()
    digits = _phone_tail("".join(ch for ch in text if ch.isdigit()))
    found = []
    for client in clients:
        if lowered in (client.full_name or "").lower():
            found.append(client)
            continue
        if digits and _phone_tail(
            "".join(ch for ch in (client.phone or "") if ch.isdigit())
        ).endswith(digits):
            found.append(client)
    return found


def _phone_tail(digits: str) -> str:
    """Отбрасывает ведущую 8/7 у номера длиннее 10 цифр: `89001234567`,
    `79001234567` и `9001234567` — один и тот же человек."""
    if len(digits) > 10 and digits[0] in "78":
        return digits[1:]
    return digits


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


def _format_due(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "без срока"


def update_balance_due_date(
    db: Session, client: Client, payload: ClientBalanceDueDateUpdate, actor: User
) -> Client:
    """Срок оплаты остатка по договору (0084-j) — вводится вручную для
    планов с остатком. Не зависит от фиксации документных данных: срок часто
    договаривают уже после подписания договора.

    Открытая задача «принять оплату после получения» получает этот срок
    дедлайном; перенос пишется в журнал задачи так же, как перенос срока у
    задач по клиенту."""
    if client.payment_plan == PaymentPlan.FULL_PREPAYMENT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="У клиента полная предоплата — остатка «после получения» нет",
        )
    if client.balance_paid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Остаток уже принят")
    was = client.balance_due_date
    client.balance_due_date = payload.balance_due_date
    db.flush()
    if was != payload.balance_due_date:
        task = _open_balance_payment_task(db, client.id)
        if task is not None:
            task.deadline = balance_due_deadline(payload.balance_due_date) if payload.balance_due_date else None
            db.flush()
            task_service.add_report(
                db,
                task,
                actor,
                kind=TaskReportKind.DEADLINE_SHIFT,
                comment=(
                    f"Срок оплаты остатка изменён с {_format_due(was)} "
                    f"на {_format_due(payload.balance_due_date)}"
                ),
            )
    return client


def _set_document_file(db: Session, client: Client, field: str, file_id: int) -> Client:
    if client.documents_locked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Документные данные уже зафиксированы")
    setattr(client, field, file_id)
    db.flush()
    return client


# Договор и приложение — два документа с общими правилами источника и
# проверки (0084-i). Подпись и окончание причастия («проверен» /
# «проверено») — для сообщений гейта и отказов.
CONTRACT_DOCUMENT_LABELS = {
    "contract": ("Договор", ""),
    "contract_appendix": ("Приложение к договору", "о"),
}


def _mark_contract_document_replaced(client: Client, document: str, source: ContractSource) -> None:
    """Новый файл договора/приложения: источник — какой пришёл, прежняя
    отметка проверки к новому файлу не относится и сбрасывается. Флаг
    `verification_required` ставится только здесь — так гейт отличает файлы,
    приложенные после ввода проверки, от старых (см. transition_stage)."""
    setattr(client, f"{document}_source", source)
    setattr(client, f"{document}_verification_required", True)
    setattr(client, f"{document}_verified_by_id", None)
    setattr(client, f"{document}_verified_at", None)
    setattr(client, f"{document}_verification_note", None)


def set_contract_files(db: Session, client: Client, contract_file_id: int, appendix_file_id: int) -> Client:
    """Обычный путь загрузки (0061): договор и приложение к договору одним
    действием на фронте — нет эндпоинта на один без другого. Загрузка не
    равна проверке (0084-i): оба документа становятся «загружен, не
    проверен»."""
    if client.documents_locked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Документные данные уже зафиксированы")
    client.contract_file_id = contract_file_id
    client.contract_appendix_file_id = appendix_file_id
    _mark_contract_document_replaced(client, "contract", ContractSource.UPLOADED)
    _mark_contract_document_replaced(client, "contract_appendix", ContractSource.UPLOADED)
    db.flush()
    return client


def set_contract_file(db: Session, client: Client, file_id: int, *, source: ContractSource) -> Client:
    """Точечная установка одного файла — для сценариев вроде генерации
    документа Мариной по одному, где второй документ приходит отдельным
    вызовом. Гейт стадии всё равно требует оба (contract_file_id и
    contract_appendix_file_id) — см. _DOCUMENTS_REQUIRED."""
    _set_document_file(db, client, "contract_file_id", file_id)
    _mark_contract_document_replaced(client, "contract", source)
    db.flush()
    return client


def set_contract_appendix_file(db: Session, client: Client, file_id: int, *, source: ContractSource) -> Client:
    _set_document_file(db, client, "contract_appendix_file_id", file_id)
    _mark_contract_document_replaced(client, "contract_appendix", source)
    db.flush()
    return client


def _other_document_editor_exists(db: Session, actor: User) -> bool:
    """Есть ли, кроме `actor`, активный пользователь с правом правки
    документов клиента (EDIT и выше в «Клиентах», администратор — всегда)."""
    return any(
        user.id != actor.id and user.access_level(Module.CLIENTS) >= AccessLevel.EDIT
        for user in user_service.users_with_access(db, Module.CLIENTS)
    )


def verify_contract_document(db: Session, client: Client, payload: ClientContractVerify, actor: User) -> Client:
    """Отметка «проверен» у договора или приложения (0084-i): кто, когда и
    что сверено. Заметка обязательна — без неё отметка ничего не говорит.

    Правила (0084 → «Принятые решения», п.2):
    - сгенерированный Мариной документ отметить нельзя — это черновик, а не
      подписанный договор, гейт его не пропускает ни при каком варианте;
    - свой же загруженный файл отметить нельзя, если в системе есть другой
      пользователь с правом правки документов клиента.

    Фиксация документных данных (`documents_locked_at`) отметку не
    запрещает: проверка не меняет файл, а у договоров, приложенных до
    0084-i, её иначе не поставить."""
    document = payload.document
    label, ending = CONTRACT_DOCUMENT_LABELS[document]
    note = payload.note.strip()
    if not note:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Напишите, что сверено: стороны, сумма, график оплаты, модель дома",
        )
    asset = getattr(client, f"{document}_file")
    if asset is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label}: файл не приложен")
    if getattr(client, f"{document}_source") == ContractSource.GENERATED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} сгенерирован{ending} Мариной — это черновик. Загрузите подписанный файл и проверьте его",
        )
    if asset.uploaded_by_id == actor.id and _other_document_editor_exists(db, actor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{label}: файл загружен вами — проверить его должен другой сотрудник",
        )
    setattr(client, f"{document}_verified_by_id", actor.id)
    setattr(client, f"{document}_verified_at", datetime.now(timezone.utc))
    setattr(client, f"{document}_verification_note", note)
    db.flush()
    return client


def _contract_gate_error(client: Client) -> str | None:
    """Гейт выхода из «согласования» по договору и приложению (0084-i).

    - Сгенерированный Мариной документ не проходит никогда.
    - Файл, приложенный после ввода проверки (`verification_required`), —
      только с отметкой «проверен».
    - Старый файл без отметки гейт пропускает: для него карточка показывает
      предупреждение «не проверен»."""
    for document, (label, ending) in CONTRACT_DOCUMENT_LABELS.items():
        if getattr(client, f"{document}_source") == ContractSource.GENERATED:
            return f"{label} сгенерирован{ending} Мариной — приложите подписанный документ"
        if getattr(client, f"{document}_verification_required") and getattr(client, f"{document}_verified_at") is None:
            return f"{label} не проверен{ending} — отметьте проверку в карточке клиента"
    return None


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
    """Удалить клиента и его заметки (каскад на уровне БД).

    Можно до производства (цикл `client` — в т.ч. тестового или ошибочно
    заведённого клиента, 0095) и после завершения цикла. Отказ 409, пока дом в
    производстве или на монтаже, есть незакрытые задачи по клиенту или проводки
    — по умолчанию запрет, а не тихий каскад (0030-a).

    Цикл до производства пустой — удаляется вместе с клиентом, чтобы в «Циклах»
    не висел цикл без клиента. Завершённый цикл с производством/монтажом
    остаётся исторической записью."""
    cycle = client.cycle
    if cycle.status in (CycleStatus.PRODUCTION, CycleStatus.INSTALLATION):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить клиента: дом уже в производстве или на монтаже",
        )
    # Задачу «перевести на следующую стадию» заводит сама система, она открыта
    # у любого клиента до производства — удаление она не блокирует (0095).
    # Блокируют задачи, которые ведут люди: задачи менеджера и приём остатка.
    open_task = (
        db.query(Task)
        .filter(
            Task.link_type.in_([TaskLinkType.CLIENT_BALANCE_PAYMENT, TaskLinkType.CLIENT_FOLLOWUP]),
            Task.link_id == client.id,
            Task.status != TaskStatus.DONE,
        )
        .order_by(Task.id)
        .first()
    )
    if open_task is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Нельзя удалить клиента: сначала закройте задачу «{open_task.title}»",
        )
    from app.accounting.models import Counterparty, MoneyMovement

    has_money_movements = (
        db.query(MoneyMovement.id).filter(MoneyMovement.client_id == client.id).first() is not None
    )
    if has_money_movements:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить клиента: есть проводки по клиенту",
        )
    # привязка контрагента к клиенту справочная; FK без ondelete уронил бы удаление
    db.query(Counterparty).filter(Counterparty.client_id == client.id).update(
        {Counterparty.client_id: None}, synchronize_session=False
    )
    for task in _open_stage_tasks(db, client.id):
        task_service.force_close(db, task)
    drop_cycle = cycle.status == CycleStatus.CLIENT and not cycle.productions and cycle.installation is None
    db.delete(client)
    db.flush()
    if drop_cycle:
        db.delete(cycle)
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
# Подписи как в карточке клиента (DocumentPanel) — для ошибки перехода (0094).
_DOCUMENT_FIELD_LABELS = {
    "order_type": "Проект",
    "final_price": "Итоговая цена",
    "installation_address": "Адрес установки",
    "contract_file_id": "Договор",
    "contract_appendix_file_id": "Приложение к договору",
    "ar_file_id": "АР",
    "kr_file_id": "КР",
}
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
    blocker = _blocking_followup_task(db, client.id)
    if blocker is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Сначала закройте задачу «{blocker.title}»",
        )
    next_stage = _next_stage(client.stage)

    # DISCUSSION requires nothing (0044 removed the wishes/area/price/layout
    # "project" group that used to be filled and locked here) — the stage
    # still exists in the pipeline, it just no longer gates anything.

    if client.stage == ClientStage.APPROVAL:
        missing = [_DOCUMENT_FIELD_LABELS[f] for f in _DOCUMENTS_REQUIRED if getattr(client, f) is None]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Не заполнены поля в карточке клиента: {', '.join(missing)}",
            )
        contract_error = _contract_gate_error(client)
        if contract_error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=contract_error)
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


# --- Задачи менеджера по клиенту (0079-d) -------------------------------


def _followup_tasks_query(db: Session, client_id: int):
    return db.query(Task).filter(
        Task.link_type == TaskLinkType.CLIENT_FOLLOWUP,
        Task.link_id == client_id,
    )


def open_followup_tasks(db: Session, client_id: int) -> list[Task]:
    return (
        _followup_tasks_query(db, client_id)
        .filter(Task.status != TaskStatus.DONE)
        .order_by(Task.id.desc())
        .all()
    )


def _blocking_followup_task(db: Session, client_id: int) -> Task | None:
    for task in open_followup_tasks(db, client_id):
        if (task.link_meta or {}).get("blocking", True):
            return task
    return None


def get_followup_task_or_404(db: Session, client: Client, task_id: int) -> Task:
    task = (
        _followup_tasks_query(db, client.id).filter(Task.id == task_id).first()
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача по клиенту не найдена")
    return task


def create_followup_task(db: Session, client: Client, payload: ClientTaskCreate, actor: User) -> Task:
    """Завести задачу менеджера по клиенту: связаться, выслать каталог,
    уточнить по ипотеке. Срок обязателен — без него задача теряется, поэтому
    он в схеме не опционален. Исполнители по умолчанию — тот, кто ставит."""
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Напишите, что нужно сделать")
    return task_service.create_task(
        db,
        title=title,
        description=payload.description,
        deadline=payload.deadline,
        # Проверку, что такие пользователи есть, делает сам create_task.
        assignee_ids=payload.assignee_ids or [actor.id],
        link_type=TaskLinkType.CLIENT_FOLLOWUP,
        link_id=client.id,
        link_meta={"stage": client.stage.value, "blocking": payload.blocking},
    )


def shift_followup_deadline(
    db: Session, task: Task, payload: ClientTaskDeadlineUpdate, actor: User
) -> Task:
    """Перенести срок задачи с причиной. Причина обязательна: по ней потом
    видно, почему клиент стоит на стадии."""
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Напишите причину переноса срока")
    if task.status == TaskStatus.DONE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Задача закрыта — срок не переносят")
    was = task.deadline.strftime("%d.%m.%Y") if task.deadline else "без срока"
    task.deadline = payload.deadline
    db.flush()
    task_service.add_report(
        db,
        task,
        actor,
        kind=TaskReportKind.DEADLINE_SHIFT,
        comment=f"Срок перенесён с {was} на {payload.deadline.strftime('%d.%m.%Y')}: {reason}",
    )
    return task


def close_followup_task(
    db: Session, client: Client, task: Task, payload: ClientTaskClose, actor: User
) -> Task:
    """Закрыть задачу описанием решения и, если передана, сразу завести
    вытекающую — обычный ход работы с клиентом: «дозвонился, просит каталог»
    → «выслать каталог»."""
    resolution = payload.resolution.strip()
    if not resolution:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Опишите, чем закончилась задача"
        )
    task_service.close_with_resolution(db, task, actor, comment=resolution)
    if payload.next_task is not None:
        create_followup_task(db, client, payload.next_task, actor)
    return task


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


def _open_balance_payment_task(db: Session, client_id: int) -> Task | None:
    return (
        db.query(Task)
        .filter(
            Task.link_type == TaskLinkType.CLIENT_BALANCE_PAYMENT,
            Task.link_id == client_id,
            Task.status != TaskStatus.DONE,
        )
        .order_by(Task.id.desc())
        .first()
    )


def _create_balance_payment_task(db: Session, client: Client) -> None:
    assignees = user_service.users_with_access(db, Module.CLIENTS)
    task = task_service.create_link_task(
        db,
        title=f"Клиент «{client.full_name}»: принять оплату после получения (остаток)",
        link_type=TaskLinkType.CLIENT_BALANCE_PAYMENT,
        link_id=client.id,
        assignees=assignees,
    )
    # Срок остатка (0084-j), если его уже указали до старта производства.
    if client.balance_due_date is not None:
        task.deadline = balance_due_deadline(client.balance_due_date)
        db.flush()


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
