"""ИИ-генерация шаблона графа этапов производства по КР (0066-d): draft-шаблон
с обязательной ссылкой на страницу у каждой составляющей, переиспользование
подтверждённого шаблона той же модели дома без обращения к ИИ, запрет правки
после confirm, детерминированный отказ без ANTHROPIC_API_KEY.
"""

from pathlib import Path

from app.clients.models import Client
from app.common.module_access import Module
from app.core.config import settings
from app.cycle.models import Cycle
from app.production import kr_extraction as kr_extraction_module
from app.production import stage_template_service
from app.production.stage_templates import ProductionStageTemplate, TemplateStatus

SAMPLE_KR = Path(__file__).resolve().parents[2] / "sources" / "АР КР и Договор" / "КР_1 блок6.pdf"

FAKE_GRAPH = {
    "blocks": [
        {
            "name": "Фундамент",
            "description": "Свайно-винтовой фундамент",
            "sequence": 1,
            "depends_on_sequence": [],
            "kr_page_refs": [{"page_number": 3, "note": "план фундамента"}],
            "tasks": [
                {"title": "Завинтить сваи", "description": None, "kr_page_ref": {"page_number": 3, "note": None}},
            ],
            "materials": [
                {"name": "Свая винтовая 108мм", "unit": "шт", "kr_page_ref": {"page_number": 3, "note": None}},
            ],
        },
        {
            "name": "Каркас",
            "description": "Каркас первого этажа",
            "sequence": 2,
            "depends_on_sequence": [1],
            "kr_page_refs": [{"page_number": 7, "note": "узел каркаса"}],
            "tasks": [
                {"title": "Собрать каркас", "description": None, "kr_page_ref": {"page_number": 7, "note": None}},
            ],
            "materials": [
                {"name": "Брус 150х150", "unit": "м3", "kr_page_ref": {"page_number": 7, "note": None}},
            ],
        },
    ]
}


def _make_client(db, house_model_key=None, houses_count=1):
    cycle = Cycle()
    db.add(cycle)
    db.flush()
    client = Client(
        cycle_id=cycle.id, full_name="Клиент шаблона", phone="+79160000000", email="tmpl@example.com",
        house_model_key=house_model_key,
    )
    db.add(client)
    db.flush()
    return client


def _make_extraction(db, client, texts):
    from app.production.models import KrExtraction

    pages = [{"page_number": i + 1, "text": text, "image_file_id": 1} for i, text in enumerate(texts)]
    record = KrExtraction(client_id=client.id, pages=pages)
    db.add(record)
    db.flush()
    return record


def test_generate_creates_draft_with_page_refs(db, make_user, monkeypatch):
    client = _make_client(db)
    _make_extraction(db, client, ["план фундамента, страница 3", "узел каркаса, страница 7"])
    db.commit()

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    calls = {"n": 0}

    def fake_call_ai(pages_payload):
        calls["n"] += 1
        return FAKE_GRAPH

    monkeypatch.setattr(stage_template_service, "_call_ai", fake_call_ai)

    template = stage_template_service.generate_or_reuse_template(db, client)
    db.commit()

    assert calls["n"] == 1
    assert template.status == TemplateStatus.DRAFT
    assert len(template.blocks) == 2
    foundation, frame = sorted(template.blocks, key=lambda b: b.sequence)
    assert foundation.kr_page_refs == [{"page_number": 3, "note": "план фундамента"}]
    assert foundation.tasks[0].kr_page_ref["page_number"] == 3
    assert foundation.materials[0].kr_page_ref["page_number"] == 3
    assert frame.depends_on_ids == [foundation.id]


def test_reuse_confirmed_template_without_calling_ai(db, make_user, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    calls = {"n": 0}

    def fake_call_ai(pages_payload):
        calls["n"] += 1
        return FAKE_GRAPH

    monkeypatch.setattr(stage_template_service, "_call_ai", fake_call_ai)

    # Первый клиент этой модели дома — генерация с нуля.
    from app.house_models.models import HouseModelCard, HouseModelConfirmation, HouseModelKind

    model = HouseModelCard(
        key="DH-64", title="DH-64", kind=HouseModelKind.CATALOG,
        confirmation=HouseModelConfirmation.NONE, confirmation_label="Не подтверждено",
        source_note_path="test/DH-64.md",
    )
    db.add(model)
    db.flush()
    first_client = _make_client(db, house_model_key="DH-64")
    _make_extraction(db, first_client, ["текст страницы 1"])
    db.commit()

    template = stage_template_service.generate_or_reuse_template(db, first_client)
    admin = make_user(admin=True)
    stage_template_service.confirm_template(db, template, admin)
    db.commit()
    assert calls["n"] == 1

    # Второй клиент той же модели дома — переиспользование, без обращения к ИИ.
    second_client = _make_client(db, house_model_key="DH-64")
    db.commit()
    reused = stage_template_service.generate_or_reuse_template(db, second_client)
    db.commit()

    assert calls["n"] == 1
    assert reused.id == template.id
    assert reused.status == TemplateStatus.CONFIRMED


def test_patch_rejected_after_confirm(db, make_user, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(stage_template_service, "_call_ai", lambda pages_payload: FAKE_GRAPH)

    client = _make_client(db)
    _make_extraction(db, client, ["текст страницы 1"])
    db.commit()

    template = stage_template_service.generate_or_reuse_template(db, client)
    db.commit()
    block = template.blocks[0]

    from app.production.schemas import TemplateBlockPatch

    stage_template_service.update_block(db, template, block, TemplateBlockPatch(name="Поправленное имя"))
    db.commit()
    assert template.status == TemplateStatus.REVIEWED

    admin = make_user(admin=True)
    stage_template_service.confirm_template(db, template, admin)
    db.commit()
    assert template.status == TemplateStatus.CONFIRMED

    try:
        stage_template_service.update_block(db, template, block, TemplateBlockPatch(name="Ещё правка"))
        assert False, "должен был отклонить правку подтверждённого шаблона"
    except Exception as error:
        assert getattr(error, "status_code", None) == 409


def test_generate_without_api_key_fails_cleanly(db, make_user):
    assert not settings.anthropic_api_key
    client = _make_client(db)
    _make_extraction(db, client, ["текст страницы 1"])
    db.commit()

    try:
        stage_template_service.generate_or_reuse_template(db, client)
        assert False, "без ключа ИИ генерация должна отказать явно"
    except Exception as error:
        assert getattr(error, "status_code", None) == 422

    assert db.query(ProductionStageTemplate).count() == 0


def test_full_http_flow_generate_patch_confirm(api, db, make_user, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(stage_template_service, "_call_ai", lambda pages_payload: FAKE_GRAPH)

    client = _make_client(db)
    _make_extraction(db, client, ["текст страницы 1"])
    db.commit()

    worker = api(make_user(Module.PRODUCTION))
    gen = worker.post("/api/production/stage-templates/generate", params={"client_id": client.id})
    assert gen.status_code == 201
    body = gen.json()
    assert body["status"] == "draft"
    template_id = body["id"]
    block = body["blocks"][0]
    assert block["kr_page_refs"][0]["page_number"] == 3
    assert block["tasks"][0]["kr_page_ref"]["page_number"] == 3

    patch = worker.patch(
        f"/api/production/stage-templates/{template_id}/blocks/{block['id']}",
        json={"name": "Фундамент (правка)"},
    )
    assert patch.status_code == 200
    assert patch.json()["status"] == "reviewed"

    confirm = worker.post(f"/api/production/stage-templates/{template_id}/confirm")
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "confirmed"

    rejected = worker.patch(
        f"/api/production/stage-templates/{template_id}/blocks/{block['id']}",
        json={"name": "Ещё правка"},
    )
    assert rejected.status_code == 409


def test_generate_requires_kr_extraction_first(db, make_user, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    client = _make_client(db)
    db.commit()

    try:
        stage_template_service.generate_or_reuse_template(db, client)
        assert False, "без разбора КР генерация должна отказать явно"
    except Exception as error:
        assert getattr(error, "status_code", None) == 400


def test_generate_on_real_kr_sample_sends_extracted_text_to_ai(db, make_user, monkeypatch):
    """Сквозной путь на реальном образце (0066-c → 0066-d): постраничный разбор
    настоящего PDF из sources/, затем генерация шаблона с мокнутым Anthropic-
    клиентом — payload, отправляемый в ИИ, должен состоять из реально
    извлечённого из файла текста, не из придуманных данных."""
    assert SAMPLE_KR.is_file(), "реальный образец КР должен лежать в sources/"

    from app.common.files import FileAsset, FilePurpose

    admin = make_user(admin=True)
    client = _make_client(db)
    asset = FileAsset(
        filename=SAMPLE_KR.name, content_type="application/pdf", path_on_disk=str(SAMPLE_KR),
        purpose=FilePurpose.CONSTRUCTIVE_DECISIONS, uploaded_by_id=admin.id,
    )
    db.add(asset)
    db.flush()
    client.kr_file_id = asset.id
    db.commit()

    kr_extraction_module.run_kr_extraction(db, client, admin)
    db.commit()

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    captured_payload = {}

    def fake_call_ai(pages_payload):
        captured_payload["pages"] = pages_payload
        return FAKE_GRAPH

    monkeypatch.setattr(stage_template_service, "_call_ai", fake_call_ai)

    template = stage_template_service.generate_or_reuse_template(db, client)
    db.commit()

    assert template.status == TemplateStatus.DRAFT
    assert len(captured_payload["pages"]) > 0
    # Текст в payload реально пришёл из файла (не выдуман) — например, штамп
    # титульного листа КР этого образца.
    assert any("Конструктивный раздел" in p["text"] for p in captured_payload["pages"])
