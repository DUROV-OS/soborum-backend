"""Раздел «Бухгалтерия» в детерминированной сводке «Пульса»
(app.dashboard.overview) — черновики проводок как реальный, не выдуманный
сигнал внимания (используется разделом «Работа» вместо замоканного совета).
"""

from app.accounting.schemas import MoneyMovementCreate
from app.accounting.service import create_money_movement
from app.common.module_access import Module
from app.dashboard.overview import generate_today


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
