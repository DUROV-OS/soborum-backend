from app.agents import connectors, context
from app.agents.ids import DAILY_QUESTIONS, AgentId
from app.agents.legal import scan
from app.agents.runtime import _rank_hits, run_task
from app.agents.shift import run_shift
from app.agents.types import ContextHit, LegalCategory, LegalVerdict
from app.clients.models import Client, ClientStage
from app.common.module_access import Module
from app.core.config import settings
from app.cycle.models import Cycle


def _seed_company(db):
    """A few clients across stages so the DurovOS-database snapshot has real rows."""
    specs = [
        (ClientStage.LEAD, None, None),
        (ClientStage.DISCUSSION, 5_000_000, None),
        (ClientStage.APPROVAL, 6_000_000, 5_400_000),  # 10% скидка — сверх лимита
        (ClientStage.PAYMENT, 4_000_000, 4_000_000),  # ещё не оплачено
    ]
    for i, (stage, estimated, final) in enumerate(specs):
        cycle = Cycle()
        db.add(cycle)
        db.flush()
        db.add(
            Client(
                cycle_id=cycle.id,
                stage=stage,
                full_name=f"Клиент {i}",
                phone=f"+7000000{i:04d}",
                email=f"client{i}@example.com",
                contacts=[],
                estimated_price=estimated,
                final_price=final,
            )
        )
    db.commit()


def _write_vault(root):
    always = {
        "00_Agent/Constitution.md": "Окончательная цена — только человек. Продаём предсказуемость.",
        "00_Agent/Operating_Principles.md": "Скидка автономно до 5%.",
        "00_Agent/Legal_Risk_Filter.md": "Ворованную информацию конкурентов использовать нельзя.",
        "00_Agent/Shared_Company_Context.md": "База знаний — источник истины.",
        "00_Agent/Agent_Roster_MVP.md": "Восемь агентов MVP.",
        "02_Business/00_Decision_Log/MOC_Decision_Log.md": "Журнал решений владельца.",
        "02_Business/01_Production/Module.md": "Цех собирает модуль. Срок изготовления считает производственник.",
        "02_Business/07_Logistics_and_Supply_Chain/Stock.md": "Материалы для ближайшего модуля проверяет кладовщик.",
    }
    for rel, body in always.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        title = path.stem.replace("_", " ")
        path.write_text(f"---\ntitle: {title}\nkind: fact\nstatus: draft\n---\n\n{body}\n", encoding="utf-8")


def test_stolen_competitor_data_is_blocked():
    decision = scan("Можно ли использовать ворованную информацию конкурентов?")
    assert decision.verdict == LegalVerdict.BLOCK
    assert any(f.category == LegalCategory.COMPETITOR_INTEL_ILLEGAL for f in decision.findings)


def test_leaked_competitor_base_is_blocked():
    decision = scan("Возьмём слитую базу клиентов конкурента и прозвоним")
    assert decision.verdict == LegalVerdict.BLOCK


def test_open_competitor_price_is_not_automatically_blocked():
    decision = scan("Сравни открытый прайс конкурента с нашего сайта")
    assert decision.verdict != LegalVerdict.BLOCK


def test_discount_escalates():
    decision = scan("Клиенту нужна скидка 15% и окончательная цена сегодня")
    assert decision.verdict == LegalVerdict.ESCALATE_HUMAN
    assert any(f.category == LegalCategory.PRICING_AUTHORITY for f in decision.findings)


def test_risk_word_is_not_a_lawsuit():
    assert scan("Какое отклонение создаёт риск на площадке?").verdict == LegalVerdict.ALLOW
    assert scan("Подадим иск к подрядчику").verdict == LegalVerdict.ESCALATE_HUMAN


def test_shift_daily_questions_do_not_self_escalate():
    for question in DAILY_QUESTIONS.values():
        assert scan(question).verdict == LegalVerdict.ALLOW, question


def test_clean_ops_question_is_allowed():
    decision = scan("Каких материалов не хватает на складе для ближайшего модуля?")
    assert decision.verdict == LegalVerdict.ALLOW
    assert decision.findings == []


def test_block_is_not_released_and_skips_commercial_work():
    result = run_task("Используем ворованную информацию конкурентов для оффера")
    assert result.released is False
    assert result.legal.verdict == LegalVerdict.BLOCK
    assert result.opinions
    assert all(o.agent == "lawyer" for o in result.opinions)


def test_production_and_warehouse_route_together():
    result = run_task("Цех не успевает модуль, каких материалов не хватает на складе?")
    assert "production" in result.route.specialists
    assert "warehouse" in result.route.specialists
    assert result.released is True


def test_worker_can_create_run_but_cannot_read_stats(api, make_user):
    worker = api(make_user(Module.PRODUCTION))
    created = worker.post("/api/agents/runs", json={"text": "Каких материалов не хватает на складе?"})
    assert created.status_code == 200
    body = created.json()
    assert body["legal_verdict"] == "allow"
    assert body["released"] is True
    assert "склад" in body["reply"].lower() or "кладовщик" in body["reply"].lower()
    assert worker.get("/api/agents/stats").status_code == 403
    assert worker.get("/api/agents/runs").status_code == 403


def test_admin_sees_live_stats_not_seed(api, make_user):
    client = api(make_user(admin=True))
    empty = client.get("/api/agents/stats")
    assert empty.status_code == 200
    assert empty.json()["totals"]["runs"] == 0
    assert empty.json()["traces"] == []

    blocked = client.post(
        "/api/agents/runs",
        json={"text": "Используем ворованную информацию конкурентов в рекламе"},
    )
    assert blocked.status_code == 200
    assert blocked.json()["legal_verdict"] == "block"
    assert blocked.json()["released"] is False

    stats = client.get("/api/agents/stats").json()
    assert stats["totals"]["runs"] == 1
    assert stats["totals"]["blocked"] == 1
    assert stats["legal"]["block"] == 1
    assert stats["traces"][0]["legal"] == "block"
    assert "юрист" in stats["traces"][0]["agents"]


def test_empty_text_is_rejected(api, make_user):
    client = api(make_user(admin=True))
    assert client.post("/api/agents/runs", json={"text": "  "}).status_code == 422


def test_run_cites_local_vault_checkout(tmp_path):
    _write_vault(tmp_path)
    result = run_task("Цех не успевает модуль, каких материалов не хватает на складе?", vault_root=str(tmp_path))
    assert result.released is True
    assert "Источники:" in result.reply
    assert "Stock.md" in result.reply or "материалов" in result.reply.lower()
    assert any(hit.path and hit.path.endswith("Stock.md") for hit in result.context.hits)


def test_gather_reads_durovos_database(db, tmp_path):
    _write_vault(tmp_path)
    _seed_company(db)
    pack = context.gather("что с оплатами", [AgentId.FINANCE], str(tmp_path), db)
    live = [hit for hit in pack.hits if hit.source == "db"]
    assert live
    assert any((hit.path or "") == "durovos/finance" for hit in live)
    assert any("портфель" in hit.excerpt.lower() or "не оплачено" in hit.excerpt.lower() for hit in live)


def test_gather_does_not_invent_crm_or_moysklad(db, tmp_path):
    _write_vault(tmp_path)
    pack = context.gather("какие сделки зависли в CRM и МойСкладе", [AgentId.SALES], str(tmp_path), db)
    assert not any(hit.source in {"crm", "warehouse"} for hit in pack.hits)
    assert not any("moysklad" in (hit.path or "").lower() for hit in pack.hits)
    # Пустая база — честные нули, а не выдуманные сделки.
    db_hits = [hit for hit in pack.hits if hit.source == "db"]
    assert db_hits
    assert "клиентов в базе 0" in db_hits[0].excerpt.lower()


def test_finance_stance_uses_database(db, tmp_path):
    _write_vault(tmp_path)
    _seed_company(db)
    draft = run_shift(db, vault_root=str(tmp_path))
    finance = next(item for item in draft.items if item.agent == AgentId.FINANCE)
    assert finance.has_live_data is True
    assert "нет данных" not in finance.stance.lower()
    assert "портфель" in finance.stance.lower() or "оплач" in finance.stance.lower()


def test_role_reads_its_section_from_database(db, tmp_path):
    _write_vault(tmp_path)
    _seed_company(db)
    draft = run_shift(db, vault_root=str(tmp_path))
    sales = next(item for item in draft.items if item.agent == AgentId.SALES)
    assert sales.has_live_data is True
    assert "нет данных" not in sales.stance.lower()
    assert "клиент" in sales.stance.lower()


def test_role_without_database_is_marked_no_data(tmp_path):
    _write_vault(tmp_path)
    draft = run_shift(vault_root=str(tmp_path))  # no db session → no live snapshot
    sales = next(item for item in draft.items if item.agent == AgentId.SALES)
    assert sales.has_live_data is False
    assert "нет данных" in sales.stance.lower()


def test_rank_puts_db_facts_before_vault():
    hits = [
        ContextHit(source="vault", title="Конституция", excerpt="правило", path="00_Agent/Constitution.md"),
        ContextHit(source="db", title="База DurovOS · Финансы", excerpt="портфель 0 ₽", path="durovos/finance"),
    ]
    assert _rank_hits(AgentId.FINANCE, hits)[0].source == "db"


def test_live_charts_come_from_database(db):
    _seed_company(db)
    charts = {chart["id"]: chart for chart in connectors.live_charts(db)}
    # Клиент на стадии оплаты без оплаты → денежный график финансиста.
    assert "finance_money" in charts
    assert charts["finance_money"]["agents"] == ["finance"]
    assert charts["finance_money"]["unit"] == "₽"
    assert charts["finance_money"]["bars"][0]["value"] > 0


def test_live_briefing_text_from_database(db):
    _seed_company(db)
    text = connectors.live_briefing_text(db)
    assert "Живой срез базы DurovOS" in text
    assert "клиентов в базе" in text.lower()
    assert "заказы покупателей" not in text.lower()


def test_proxy_base_url_drops_v1_suffix():
    from app.core.llm import normalize_anthropic_base_url

    assert (
        normalize_anthropic_base_url("https://shprotoness-ai.jq9gfk.workers.dev/v1")
        == "https://shprotoness-ai.jq9gfk.workers.dev"
    )


def test_shift_does_not_crash_when_claude_key_is_set(monkeypatch, tmp_path):
    _write_vault(tmp_path)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-not-a-real-key")
    draft = run_shift(vault_root=str(tmp_path))
    assert len(draft.items) == 8
    assert draft.claude_used is False


def test_worker_cannot_start_shift(api, make_user):
    worker = api(make_user(Module.PRODUCTION))
    assert worker.post("/api/agents/shifts").status_code == 403


def test_admin_shift_has_eight_roles_cross_review_and_approval_queue(api, make_user):
    client = api(make_user(admin=True))
    created = client.post("/api/agents/shifts")
    assert created.status_code == 200
    body = created.json()
    assert len(body["items"]) == 8
    assert {item["agent_id"] for item in body["items"]} == {
        "coordinator",
        "sales",
        "marketer",
        "production",
        "warehouse",
        "finance",
        "lawyer",
        "engineer",
    }
    sales = next(item for item in body["items"] if item["agent_id"] == "sales")
    assert sales["reviews"]
    # Финансист по-прежнему смотрит черновик продажника, но больше не выдумывает
    # эскалацию по цене/скидке — это не мок, а реальный кросс-обзор.
    assert any(review["reviewer"] == "finance" for review in sales["reviews"])
    assert not any(review["escalate"] for review in sales["reviews"])
    # Дежурные вопросы не создают юридического риска → очередь «что решить вам» пуста.
    assert body["approvals"] == []
    assert body["verdict"] == "allow"
    assert body["claude_used"] is False
    assert "ничего не нужно" in body["summary"]
    assert "charts" in body
    assert body["next_tick_at"]

    latest = client.get("/api/agents/shifts/latest")
    assert latest.status_code == 200
    assert latest.json()["id"] == body["id"]

    stats = client.get("/api/agents/stats").json()
    assert stats["shifts"] == 1


def test_no_mock_pricing_or_lawyer_lines_in_shift(api, make_user):
    client = api(make_user(admin=True))
    body = client.post("/api/agents/shifts").json()
    blob = repr(body)
    assert "скидку больше 5%" not in blob
    assert "Ворованную базу конкурента нельзя" not in blob
    assert "МойСклад" not in blob
    assert "из CRM в договор" not in blob


def test_approval_decision_flow(api, make_user, db):
    from app.agents.models import AgentApproval, AgentShift

    client = api(make_user(admin=True))
    client.post("/api/agents/shifts")
    shift = db.query(AgentShift).order_by(AgentShift.id.desc()).first()
    approval = AgentApproval(
        shift_id=shift.id,
        kind="legal",
        title="Юрист просит вас посмотреть",
        detail="Реальная эскалация от детерминированного фильтра.",
        status="pending",
    )
    db.add(approval)
    db.commit()

    decided = client.post(f"/api/agents/approvals/{approval.id}/decision", json={"status": "approved"})
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"


def test_auto_tick_skips_a_fresh_shift(api, make_user, db):
    from app.agents.models import AgentShift
    from app.agents.service import maybe_tick_shift

    client = api(make_user(admin=True))
    assert client.post("/api/agents/shifts").status_code == 200
    assert maybe_tick_shift(db) is None
    assert db.query(AgentShift).count() == 1
