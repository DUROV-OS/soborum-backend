from pathlib import Path

from app.common.file_text import extract_asset_text, model_hint
from app.common.files import FileAsset, FilePurpose


def test_ocr_reads_scanned_pdf(tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    import pymupdf

    from app.common.file_text import extract_asset_text, _needs_ocr
    from app.common.files import FileAsset, FilePurpose

    img_path = tmp_path / "scan.png"
    img = Image.new("RGB", (800, 300), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    draw.text((30, 40), "Техкарта DH-96", fill="black", font=font)
    draw.text((30, 100), "Саморезы 1250 шт", fill="black", font=font)
    img.save(img_path)

    pdf_path = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=800, height=300)
    page.insert_image(page.rect, filename=str(img_path))
    doc.save(pdf_path)
    doc.close()

    assert _needs_ocr("")
    asset = FileAsset(
        id=1,
        filename="DH96_scan.pdf",
        content_type="application/pdf",
        path_on_disk=str(pdf_path),
        purpose=FilePurpose.HOUSE_PROJECT,
        uploaded_by_id=1,
    )
    out = extract_asset_text(asset)
    assert out["source"] == "pdf+ocr"
    assert out["model_hint"] == "DH-96"
    assert "саморез" in out["text"].lower() or "1250" in out["text"]


def test_model_hint_reads_dh96_without_hyphen():
    assert model_hint("DH96_Tech_Card_Full_BOM.pdf") == "DH-96"
    assert model_hint("DH-64Чеплыгины.pdf") == "DH-64"


def test_extract_pdf_project_text(tmp_path):
    src = Path("/tmp/soborum-storage/b5ef0cbdd83a4d8ea0452383377e8a73_DH96_Tech_Card_Full_BOM.pdf")
    if not src.exists():
        return
    asset = FileAsset(
        id=1,
        filename="DH96_Tech_Card_Full_BOM.pdf",
        content_type="application/pdf",
        path_on_disk=str(src),
        purpose=FilePurpose.HOUSE_PROJECT,
        uploaded_by_id=1,
    )
    out = extract_asset_text(asset)
    assert out["model_hint"] == "DH-96"
    assert "DH-96" in out["text"]
    assert "Каркас" in out["text"] or "домокомплект" in out["text"].lower()


def test_get_client_embeds_project_spec(db, make_user, monkeypatch, tmp_path):
    from app.ai import tools as ai_tools
    from app.clients.models import Client, ClientStage
    from app.common.module_access import Module
    from app.cycle.models import Cycle, CycleStatus

    pdf = Path("/tmp/soborum-storage/b5ef0cbdd83a4d8ea0452383377e8a73_DH96_Tech_Card_Full_BOM.pdf")
    if not pdf.exists():
        return
    user = make_user(Module.AI, Module.CLIENTS, Module.PRODUCTION, admin=True)
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    asset = FileAsset(
        filename="DH96_Tech_Card_Full_BOM.pdf",
        content_type="application/pdf",
        path_on_disk=str(pdf),
        purpose=FilePurpose.HOUSE_PROJECT,
        uploaded_by_id=user.id,
    )
    db.add(asset)
    db.flush()
    client = Client(
        cycle_id=cycle.id,
        full_name="Тест",
        phone="+79001112233",
        email="test@example.com",
        stage=ClientStage.APPROVAL,
        wishes_description="kdnkndkndk",
        layout_notes="kdnkwndkw",
        house_area=3,
        house_project_file_id=asset.id,
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    payload = ai_tools._serialize_client(client)
    assert payload["project_model"] == "DH-96"
    assert payload["must_use_attached_files"] is True
    assert "DH-96" in payload["project_spec"]["text"]
    assert "kdnkndkndk" in payload["wishes_description"]
