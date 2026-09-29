"""Срок оплаты остатка и честная просрочка (0084-j):

- «оплата после получения» без отметки оплаты проходит «оплату» и не даёт
  сигналов, пока срок не наступил;
- без срока — отдельный сигнал «срок не указан», а не просрочка;
- срок прошёл, остаток не принят — «остаток просрочен» на Пульсе и в
  данных клиента для Марины;
- срок остатка — дедлайн задачи «принять оплату после получения», перенос
  срока пишется в журнал задачи;
- принятый остаток сигналов не даёт.
"""

from datetime import date, timedelta

from app.agents.connectors import _clients_line
from app.ai.tools import _get_client
from app.clients import service as client_service
from app.clients.models import BalanceState, PaymentPlan, balance_state, balance_today
from app.clients.schemas import (
    ClientBalancePaymentUpdate,
    ClientCreate,
    ClientDocumentsUpdate,
)
from app.common.module_access import AccessLevel, Module
from app.dashboard.overview import generate_today
from app.dashboard.service import _snapshot_clients
from app.tasks.models import Task, TaskLinkType, TaskReportKind, TaskStatus


def _post_payment_client_in_production(db, due: date | None = None):
    """Клиент с «оплатой после получения», проведённый через «оплату» без
    отметки поступления — план это законно разрешает."""
    client = client_service.create_client(
        db, ClientCreate(full_name="Постоплата Тест", phone="+70000000000", email="post@example.com")
    )
    for _ in range(3):  # LEAD -> DISCUSSION -> SITE_VISIT -> APPROVAL
        client_service.transition_stage(db, client)
    client.contract_file_id = 1
    client.contract_appendix_file_id = 1
    client.ar_file_id = 1
    client.kr_file_id = 1
    client_service.update_documents(db, client, ClientDocumentsUpdate(
        order_type="single", final_price=2_000_000, installation_address="г. Тест, ул. Тест, 1",
        payment_plan=PaymentPlan.POST_PAYMENT,
    ))
    client.balance_due_date = due
    client_service.transition_stage(db, client)  # APPROVAL -> PAYMENT
    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT, без is_paid
    db.commit()
    return client


def _balance_task(db, client_id):
    return (
        db.query(Task)
        .filter(Task.link_type == TaskLinkType.CLIENT_BALANCE_PAYMENT, Task.link_id == client_id)
        .one()
    )


def _client_actions(db, user):
    return {a.id for a in generate_today(db, user).actions if a.section == "clients"}


def test_post_payment_within_due_date_gives_no_signal(db, make_user):
    client = _post_payment_client_in_production(db, due=balance_today() + timedelta(days=7))
    assert client.is_paid is False
    assert balance_state(client) == BalanceState.PENDING

    snap = _snapshot_clients(db)
    assert snap["balance_pending"] == 1
    assert snap["balance_overdue"] == 0
    assert snap["balance_no_due_date"] == 0
    # Нарушения нет: ни оплаты на «оплате», ни остатка в очереди внимания.
    assert snap["awaiting_payment_confirmation"] == 0
    user = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    assert _client_actions(db, user) == set()

    # Срок остатка — дедлайн задачи приёма остатка (конец дня срока).
    task = _balance_task(db, client.id)
    assert task.deadline is not None
    assert task.deadline.date() == client.balance_due_date


def test_due_date_today_is_not_overdue(db):
    client = _post_payment_client_in_production(db, due=balance_today())
    assert balance_state(client) == BalanceState.PENDING


def test_no_due_date_is_its_own_signal_not_overdue(db, make_user):
    client = _post_payment_client_in_production(db, due=None)
    assert balance_state(client) == BalanceState.NO_DUE_DATE
    assert _balance_task(db, client.id).deadline is None

    snap = _snapshot_clients(db)
    assert snap["balance_no_due_date"] == 1
    assert snap["balance_overdue"] == 0
    user = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    assert _client_actions(db, user) == {"clients:balance_no_due_date"}


def test_past_due_date_is_overdue_on_pulse_and_for_marina(db, make_user):
    client = _post_payment_client_in_production(db, due=balance_today() - timedelta(days=1))
    assert balance_state(client) == BalanceState.OVERDUE

    snap = _snapshot_clients(db)
    assert snap["balance_overdue"] == 1
    user = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    actions = {a.id: a for a in generate_today(db, user).actions if a.section == "clients"}
    assert set(actions) == {"clients:balance_overdue"}
    assert actions["clients:balance_overdue"].tone == "danger"

    # Марина видит то же состояние в данных клиента и в сводке агентов.
    assert _get_client(db, user, client.id)["balance_state"] == "overdue"
    assert "просрочен 1" in _clients_line(snap)[1]


def test_setting_due_date_moves_task_deadline_with_journal_entry(api, make_user, db):
    client = _post_payment_client_in_production(db, due=None)
    user = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    due = balance_today() + timedelta(days=7)

    resp = api(user).patch(f"/api/clients/{client.id}/balance-due-date", json={"balance_due_date": due.isoformat()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["balance_due_date"] == due.isoformat()
    assert body["balance_state"] == "pending"

    task = _balance_task(db, client.id)
    db.refresh(task)
    assert task.deadline.date() == due
    shifts = [r for r in task.reports if r.kind == TaskReportKind.DEADLINE_SHIFT]
    assert len(shifts) == 1
    assert "без срока" in shifts[0].comment
    assert due.strftime("%d.%m.%Y") in shifts[0].comment


def test_view_only_user_cannot_set_due_date(api, make_user, db):
    client = _post_payment_client_in_production(db, due=None)
    viewer = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    resp = api(viewer).patch(
        f"/api/clients/{client.id}/balance-due-date", json={"balance_due_date": balance_today().isoformat()}
    )
    assert resp.status_code == 403


def test_full_prepayment_has_no_balance_due_date(api, make_user, db):
    client = client_service.create_client(
        db, ClientCreate(full_name="Предоплата", phone="+7", email="full@example.com")
    )
    db.commit()
    assert balance_state(client) == BalanceState.NOT_APPLICABLE
    user = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    resp = api(user).patch(
        f"/api/clients/{client.id}/balance-due-date", json={"balance_due_date": balance_today().isoformat()}
    )
    assert resp.status_code == 400


def test_paid_balance_gives_no_signal(db, make_user):
    client = _post_payment_client_in_production(db, due=balance_today() - timedelta(days=3))
    client_service.record_balance_payment(db, client, ClientBalancePaymentUpdate(balance_paid=True))
    db.commit()

    assert balance_state(client) == BalanceState.PAID
    assert _balance_task(db, client.id).status == TaskStatus.DONE
    snap = _snapshot_clients(db)
    assert (snap["balance_pending"], snap["balance_overdue"], snap["balance_no_due_date"]) == (0, 0, 0)
    user = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    assert _client_actions(db, user) == set()
