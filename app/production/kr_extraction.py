"""Постраничный разбор КР (конструктивных решений) — сырьё для ИИ-генерации
графа этапов производства ([[0066-d]]) и экрана проверки ([[0066-e]]), где
каждая страница служит «чертежом», на который ссылается конкретный предложенный
блок/задача/материал.

На каждую страницу PDF: текстовый слой через `pymupdf`, при пустом слое —
OCR через `pytesseract` по растру страницы (тот же подход, что
`app.common.file_text` использует для целого документа, здесь — постранично).

Без претензии на универсальность для произвольного стороннего КР — критерий
готовности - непустой постраничный результат на реальном образце из
`sources/АР КР и Договор/КР_1 блок6.pdf` (см. тест).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("app.production.kr_extraction")

_MIN_NATIVE_CHARS = 20


@dataclass
class KrPage:
    page_number: int
    text: str


def _tesseract_langs() -> str:
    try:
        import pytesseract

        available = set(pytesseract.get_languages(config=""))
    except Exception:
        available = set()
    langs = [lang for lang in ("rus", "eng") if lang in available]
    return "+".join(langs) if langs else "eng"


def _ocr_pixmap(pix) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return (pytesseract.image_to_string(image, lang=_tesseract_langs()) or "").strip()
    except Exception as error:  # noqa: BLE001 — OCR деградирует к пустой строке, не роняет разбор
        log.warning("OCR страницы КР не сработал: %s", error)
        return ""


def extract_kr_pages(path_on_disk: str) -> list[KrPage]:
    """Постраничный текст PDF: нативный слой, при пустом слое — OCR по растру
    страницы (`_MIN_NATIVE_CHARS` — порог "слой практически пуст")."""
    import pymupdf

    pages: list[KrPage] = []
    doc = pymupdf.open(path_on_disk)
    try:
        for index in range(len(doc)):
            page = doc[index]
            page_number = index + 1
            text = (page.get_text() or "").strip()
            if len(text) < _MIN_NATIVE_CHARS:
                pix = page.get_pixmap(alpha=False)
                ocr_text = _ocr_pixmap(pix)
                if len(ocr_text) > len(text):
                    text = ocr_text
            pages.append(KrPage(page_number=page_number, text=text))
    finally:
        doc.close()
    return pages
