from app.agents.legal import scan
from app.agents.runtime import run_task
from app.agents.types import LegalCategory, LegalVerdict
from app.common.module_access import Module


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
