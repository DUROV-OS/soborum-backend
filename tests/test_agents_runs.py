from app.agents import connectors, context
from app.agents.ids import DAILY_QUESTIONS, AgentId
from app.agents.legal import scan
from app.agents.runtime import _rank_hits, run_task
from app.agents.shift import run_shift
from app.agents.types import ContextHit, LegalCategory, LegalVerdict
from app.common.module_access import Module
from app.core.config import settings


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


def test_gather_without_tokens_does_not_invent_deals(tmp_path):
    _write_vault(tmp_path)
    pack = context.gather("какие сделки зависли", [AgentId.SALES], str(tmp_path))
    crm = [hit for hit in pack.hits if hit.source == "crm"]
    assert crm
    assert "не выдумываем" in crm[0].excerpt.lower()


def test_gather_reads_via_mcp(monkeypatch, tmp_path):
    _write_vault(tmp_path)
    monkeypatch.setattr(settings, "amocrm_mcp_url", "https://amocrm.example/mcp")
    monkeypatch.setattr(settings, "amocrm_mcp_client_id", "id")
    monkeypatch.setattr(settings, "amocrm_mcp_client_secret", "secret")
    monkeypatch.setattr(settings, "moysklad_mcp_url", "https://moysklad.example/mcp")
    monkeypatch.setattr(settings, "moysklad_mcp_client_id", "id")
    monkeypatch.setattr(settings, "moysklad_mcp_client_secret", "secret")

    def fake_call(name, arguments=None, timeout=30.0):
        if name == "get_all_deals":
            return [{"id": 22, "name": "Невзоровы", "price": 450000, "status_id": 10, "updated_at": "2026-09-07"}]
        if name == "get_stock_all":
            return {"rows": [{"name": "брус 50х150", "quantity": 4, "code": "BRUS"}]}
        if name == "list_customer_orders":
            return {"rows": [{"id": "o1", "name": "Заказ 12", "sum": 120000, "moment": "2026-09-07"}]}
        raise AssertionError(name)

    class FakeRemote:
        def call_tool(self, name, arguments=None, timeout=30.0):
            return fake_call(name, arguments, timeout)

    monkeypatch.setattr(connectors, "client_for", lambda target: FakeRemote())
    connectors.clear_cache()
    pack = context.gather(
        "какие сделки зависли и каких материалов не хватает",
        [AgentId.SALES, AgentId.WAREHOUSE],
        str(tmp_path),
    )
    assert any((hit.path or "").startswith("amocrm/leads/") for hit in pack.hits)
    assert any((hit.path or "").startswith("moysklad/stock/") for hit in pack.hits)
    assert any("Невзоровы" in hit.excerpt for hit in pack.hits)
    assert any("брус" in hit.excerpt.lower() for hit in pack.hits)


def test_gather_reads_live_amocrm_and_moysklad(monkeypatch, tmp_path):
    _write_vault(tmp_path)
    monkeypatch.setattr(settings, "amocrm_subdomain", "durov")
    monkeypatch.setattr(settings, "amocrm_long_lived_token", "token")
    monkeypatch.setattr(settings, "moysklad_token", "token")
    monkeypatch.setattr(
        connectors,
        "_amocrm_snapshot",
        lambda: ([{"id": 11, "name": "Невзоровы", "price": 450000, "status_id": 10}], {10: "переговор"}),
    )
    monkeypatch.setattr(
        connectors,
        "_moysklad_snapshot",
        lambda: (
            [{"name": "брус 50х150", "quantity": 4, "code": "BRUS", "uom": {"name": "шт"}}],
            [{"id": "o1", "name": "Заказ 12", "sum": 12000000, "moment": "2026-09-07"}],
        ),
    )
    connectors.clear_cache()
    pack = context.gather(
        "какие сделки зависли и каких материалов не хватает",
        [AgentId.SALES, AgentId.WAREHOUSE],
        str(tmp_path),
    )
    assert any((hit.path or "").startswith("amocrm/leads/") for hit in pack.hits)
    assert any((hit.path or "").startswith("moysklad/stock/") for hit in pack.hits)
    assert any("Невзоровы" in hit.excerpt for hit in pack.hits)
    assert any("брус" in hit.excerpt.lower() for hit in pack.hits)


def test_sales_stance_uses_live_crm_not_vault_story(monkeypatch, tmp_path):
    _write_vault(tmp_path)
    monkeypatch.setattr(settings, "amocrm_mcp_url", "https://amocrm.example/mcp")
    monkeypatch.setattr(settings, "amocrm_mcp_client_id", "id")
    monkeypatch.setattr(settings, "amocrm_mcp_client_secret", "secret")
    monkeypatch.setattr(
        connectors,
        "_amocrm_snapshot",
        lambda: ([{"id": 11, "name": "Невзоровы", "price": 450000, "status_id": 10, "updated_at": "2026-09-07"}], {}),
    )
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(
        "app.agents.shift._claude_stance",
        lambda *args, **kwargs: "В пакете нет данных о текущих сделках в amoCRM.",
    )
    connectors.clear_cache()
    draft = run_shift(vault_root=str(tmp_path))
    sales = next(item for item in draft.items if item.agent == AgentId.SALES)
    assert "Невзоровы" in sales.stance
    assert "нет данных" not in sales.stance.lower()


def test_rank_puts_live_crm_before_vault():
    hits = [
        ContextHit(source="vault", title="Конституция", excerpt="правило", path="00_Agent/Constitution.md"),
        ContextHit(source="crm", title="amoCRM: Невзоровы", excerpt="открыта", path="amocrm/leads/11"),
    ]
    assert _rank_hits(AgentId.SALES, hits)[0].source == "crm"


def test_live_charts_are_role_specific(monkeypatch):
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
    monkeypatch.setattr(settings, "amocrm_mcp_url", "https://amocrm.example/mcp")
    monkeypatch.setattr(settings, "amocrm_mcp_client_id", "id")
    monkeypatch.setattr(settings, "amocrm_mcp_client_secret", "secret")
    monkeypatch.setattr(settings, "moysklad_mcp_url", "https://moysklad.example/mcp")
    monkeypatch.setattr(settings, "moysklad_mcp_client_id", "id")
    monkeypatch.setattr(settings, "moysklad_mcp_client_secret", "secret")
    monkeypatch.setattr(
        connectors,
        "_amocrm_snapshot",
        lambda: (
            [{"id": 11, "name": "Невзоровы", "price": 0, "status_id": 10, "updated_at": old}],
            {},
        ),
    )
    monkeypatch.setattr(
        connectors,
        "_moysklad_snapshot",
        lambda: (
            [{"name": "брус 50х150", "quantity": -2, "code": "BRUS", "inTransit": 3}],
            [{"name": "Заказ 12", "sum": 120000, "payedSum": 0, "shippedSum": 0}],
        ),
    )
    monkeypatch.setattr(
        connectors,
        "_production_tasks",
        lambda: [{"id": "t1", "name": "012/DH-64", "moment": old}],
    )
    connectors.clear_cache()
    charts = {chart["id"]: chart for chart in connectors.live_charts(wait=True)}
    assert charts["sales_stuck"]["agents"] == ["sales"]
    assert charts["sales_stuck"]["bars"][0]["label"] == "Невзоровы"
    assert charts["finance_money"]["agents"] == ["finance"]
    assert charts["finance_money"]["unit"] == "₽"
    assert charts["warehouse_gap"]["agents"] == ["warehouse"]
    assert charts["warehouse_gap"]["bars"][0]["value"] == -2
    assert charts["production_tasks"]["agents"] == ["production"]
    assert charts["coordinator_pulse"]["agents"] == ["coordinator"]
    assert "marketer" not in {agent for chart in charts.values() for agent in chart["agents"]}
    assert "deals" not in charts
    assert "stock" not in charts


def test_live_briefing_uses_moysklad_not_empty_module(monkeypatch):
    monkeypatch.setattr(settings, "moysklad_mcp_url", "https://moysklad.example/mcp")
    monkeypatch.setattr(settings, "moysklad_mcp_client_id", "id")
    monkeypatch.setattr(settings, "moysklad_mcp_client_secret", "secret")
    monkeypatch.setattr(
        connectors,
        "_chart_sources",
        lambda: (
            [],
            [{"name": "саморезы", "quantity": -999.6}, {"name": "вентилятор", "quantity": -2}],
            [{"name": "00002", "sum": 100, "payedSum": 0, "shippedSum": 100}],
            [{"name": "0001/DH-21", "moment": "2026-01-26T20:47:00"}],
        ),
    )
    connectors.clear_cache()
    text = connectors.live_briefing_text()
    assert "минусе 2" in text
    assert "саморезы" in text
    assert "не пустой Soborbum" in text or "МойСклад" in text
    assert "достаточ" not in text


def test_stock_charts_count_every_page(monkeypatch):
    monkeypatch.setattr(settings, "moysklad_mcp_url", "https://moysklad.example/mcp")
    monkeypatch.setattr(settings, "moysklad_mcp_client_id", "id")
    monkeypatch.setattr(settings, "moysklad_mcp_client_secret", "secret")

    def fake_call(name, arguments=None, timeout=30.0):
        offset = (arguments or {}).get("offset", 0)
        if name == "get_stock_all":
            if offset == 0:
                return {"rows": [{"name": "вентилятор", "quantity": -2}] + [{"name": f"ок{i}", "quantity": 1} for i in range(99)]}
            if offset == 100:
                return {"rows": [{"name": "саморезы", "quantity": -999.6}]}
            return {"rows": []}
        if name == "list_customer_orders":
            return {"rows": []}
        if name == "list_production_tasks":
            return {"rows": []}
        raise AssertionError(name)

    class FakeRemote:
        def call_tool(self, name, arguments=None, timeout=30.0):
            return fake_call(name, arguments, timeout)

    monkeypatch.setattr(connectors, "client_for", lambda target: FakeRemote())
    connectors.clear_cache()
    connectors._charts_ready = []
    charts = {chart["id"]: chart for chart in connectors.live_charts(wait=True)}
    pulse = {bar["label"]: bar["value"] for bar in charts["coordinator_pulse"]["bars"]}
    assert pulse["Позиции в минусе"] == 2
    assert charts["warehouse_gap"]["bars"][0]["label"].startswith("саморезы")
    assert charts["warehouse_gap"]["bars"][0]["value"] == -999.6


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
    assert any(review["reviewer"] == "finance" and review["escalate"] for review in sales["reviews"])
    assert body["approvals"]
    assert any("цен" in item["title"].lower() for item in body["approvals"])
    assert not any("вопрос смены не allow" in item["title"] for item in body["approvals"])
    assert body["verdict"] == "escalate_human"
    assert body["claude_used"] is False
    assert "решить" in body["summary"] or "ничего не нужно" in body["summary"]
    assert "charts" in body
    assert body["next_tick_at"]

    latest = client.get("/api/agents/shifts/latest")
    assert latest.status_code == 200
    assert latest.json()["id"] == body["id"]

    approval_id = body["approvals"][0]["id"]
    decided = client.post(f"/api/agents/approvals/{approval_id}/decision", json={"status": "approved"})
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"

    stats = client.get("/api/agents/stats").json()
    assert stats["shifts"] == 1


def test_auto_tick_skips_a_fresh_shift(api, make_user, db):
    from app.agents.models import AgentShift
    from app.agents.service import maybe_tick_shift

    client = api(make_user(admin=True))
    assert client.post("/api/agents/shifts").status_code == 200
    assert maybe_tick_shift(db) is None
    assert db.query(AgentShift).count() == 1
