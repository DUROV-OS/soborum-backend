"""0084-e: смена агентов не выдаёт отсутствие проверки, ответ Claude без фактов
и согласование за благополучие, живые данные и исполнение."""

from app.agents import shift as shift_module
from app.agents.ids import AgentId
from app.agents.models import AgentApproval, AgentShift, AgentShiftItem
from app.agents.service import subject_hash
from app.agents.shift import run_shift
from app.clients.models import Client, ClientStage
from app.cycle.models import Cycle

PRICING_QUESTION = "Клиенту нужна скидка 15% и окончательная цена сегодня"
ANOTHER_PRICING_QUESTION = "Дадим скидку 20% и окончательная цена будет ниже прайса?"


def _seed_client(db):
    cycle = Cycle()
    db.add(cycle)
    db.flush()
    db.add(
        Client(
            cycle_id=cycle.id,
            stage=ClientStage.PAYMENT,
            full_name="Клиент на оплате",
            phone="+70000000001",
            email="payment@example.com",
            contacts=[],
            final_price=4_000_000,
        )
    )
    db.commit()


def test_non_legal_reviewers_say_not_checked(tmp_path):
    draft = run_shift(vault_root=str(tmp_path))
    reviews = [(item, review) for item in draft.items for review in item.reviews]
    ops = [review for _, review in reviews if review.reviewer != AgentId.LAWYER]
    legal = [review for _, review in reviews if review.reviewer == AgentId.LAWYER]
    assert ops and legal
    for review in ops:
        assert review.status == "not_checked"
        assert review.escalate is False
        assert review.text.startswith("Не проверено: у ")
        assert "стоп-фактора не нашёл" not in review.text
    # Юрист проверяет по-настоящему: детерминированный фильтр, статус checked_*.
    assert all(review.status in ("checked_ok", "checked_escalate") for review in legal)


def test_summary_warns_when_items_are_not_checked(tmp_path):
    draft = run_shift(vault_root=str(tmp_path))
    assert draft.approvals == []
    # Кладовщика (производственник, финансист) и юриста (координатор) реально
    # никто не проверяет → «ничего не нужно» только с оговоркой.
    assert "Сейчас от вас ничего не нужно, но 2 пункта не проверены." in draft.summary
    assert "Проверок «Не проверено»" in draft.summary or "проверок «Не проверено»" in draft.summary
    assert "без данных из системы" in draft.summary


def test_claude_without_live_facts_is_not_live_data(monkeypatch, tmp_path):
    monkeypatch.setattr(shift_module, "_claude_stance", lambda *args, **kwargs: "Факта нет, цифры не выдумываю.")
    draft = run_shift(vault_root=str(tmp_path))  # без db — живых хитов нет
    assert draft.claude_used is True
    for item in draft.items:
        assert item.has_live_data is False
        assert item.stance_source == "llm_without_facts"
    assert "8 пунктов без данных из системы" in draft.summary


def test_live_hit_is_live_data(db, tmp_path):
    _seed_client(db)
    draft = run_shift(db, vault_root=str(tmp_path))
    finance = next(item for item in draft.items if item.agent == AgentId.FINANCE)
    assert finance.has_live_data is True
    assert finance.stance_source == "live"


def test_no_source_is_marked_none(tmp_path):
    draft = run_shift(vault_root=str(tmp_path))
    assert all(item.stance_source == "none" and item.has_live_data is False for item in draft.items)


def test_api_exposes_review_status_and_stance_source(api, make_user):
    client = api(make_user(admin=True))
    body = client.post("/api/agents/shifts").json()
    for item in body["items"]:
        # С базой у ролей есть живой срез (пусть и нулевой) — это «live».
        assert item["stance_source"] in ("live", "none")
        assert item["has_live_data"] is (item["stance_source"] == "live")
        for review in item["reviews"]:
            expected = {"not_checked"} if review["reviewer"] != "lawyer" else {"checked_ok", "checked_escalate"}
            assert review["status"] in expected


def test_legacy_rows_have_no_status_or_source(api, make_user, db):
    client = api(make_user(admin=True))
    shift = AgentShift(verdict="allow", summary="Сейчас от вас ничего не нужно.", claude_used=True)
    db.add(shift)
    db.flush()
    db.add(
        AgentShiftItem(
            shift_id=shift.id,
            agent_id="sales",
            daily_question="Какие сделки зависли?",
            stance="Старый ответ Claude.",
            citations=[],
            legal_verdict="allow",
            has_live_data=True,
            reviews=[{"reviewer": "finance", "text": "Финансист видел черновик", "escalate": False, "kind": "ops"}],
        )
    )
    db.commit()
    item = client.get("/api/agents/shifts/latest").json()["items"][0]
    assert item["stance_source"] is None
    assert item["reviews"][0]["status"] is None


def test_different_questions_of_one_kind_are_separate_approvals(monkeypatch, tmp_path):
    monkeypatch.setitem(shift_module.DAILY_QUESTIONS, AgentId.SALES, PRICING_QUESTION)
    monkeypatch.setitem(shift_module.DAILY_QUESTIONS, AgentId.MARKETER, ANOTHER_PRICING_QUESTION)
    draft = run_shift(vault_root=str(tmp_path))
    pricing = [approval for approval in draft.approvals if approval.kind == "pricing"]
    assert {approval.agent for approval in pricing} == {AgentId.SALES, AgentId.MARKETER}
    titles = {approval.title for approval in pricing}
    assert len(titles) == 2
    assert any(title.startswith("Продажник: ") for title in titles)
    assert any(title.startswith("Маркетолог: ") for title in titles)
    assert all("Нужно ваше решение" not in title for title in titles)
    # (kind, пункт) уникален: у одного пункта не два согласования одного типа.
    keys = [(approval.kind, approval.agent) for approval in draft.approvals]
    assert len(keys) == len(set(keys))


def test_stale_or_missing_subject_hash_is_rejected(monkeypatch, api, make_user, db):
    monkeypatch.setitem(shift_module.DAILY_QUESTIONS, AgentId.SALES, PRICING_QUESTION)
    client = api(make_user(admin=True))
    body = client.post("/api/agents/shifts").json()
    approval = next(item for item in body["approvals"] if item["kind"] == "pricing")
    assert approval["subject_hash"]
    assert approval["subject_snapshot"]["agent"] == "sales"
    assert approval["subject_hash"] == subject_hash(approval["subject_snapshot"])
    url = f"/api/agents/approvals/{approval['id']}/decision"

    missing = client.post(url, json={"status": "approved"})
    assert missing.status_code == 409
    assert "изменился" in missing.json()["detail"]

    stale = client.post(url, json={"status": "approved", "subject_hash": "0" * 64})
    assert stale.status_code == 409
    assert "изменился" in stale.json()["detail"]
    assert db.get(AgentApproval, approval["id"]).status == "pending"

    ok = client.post(url, json={"status": "approved", "subject_hash": approval["subject_hash"]})
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"


def test_legacy_approval_without_snapshot_cannot_be_decided(api, make_user, db):
    client = api(make_user(admin=True))
    client.post("/api/agents/shifts")
    shift = db.query(AgentShift).order_by(AgentShift.id.desc()).first()
    legacy = AgentApproval(
        shift_id=shift.id,
        kind="pricing",
        title="Нужно ваше решение",
        detail="Согласование до 0084-e, без снимка.",
        status="pending",
    )
    db.add(legacy)
    db.commit()
    response = client.post(
        f"/api/agents/approvals/{legacy.id}/decision", json={"status": "approved", "subject_hash": "a" * 64}
    )
    assert response.status_code == 409
    assert db.get(AgentApproval, legacy.id).status == "pending"
