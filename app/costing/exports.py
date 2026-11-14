import base64
from html import escape
from io import BytesIO
from zipfile import ZipFile, BadZipFile

from app.costing.service import fail


def safe_cell(value):
    # Spreadsheet applications must never execute material/client names as formulas.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def workbook(headers, rows, title, company="", logo=""):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    if company:
        ws.append([safe_cell(company)])
        ws.append([safe_cell(title)])
        ws.append([])
    header_row = ws.max_row + 1 if company else 1
    ws.append([safe_cell(v) for v in headers])
    for row in rows:
        ws.append([safe_cell(v) for v in row])
    for cell in ws[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="214D43")
    for column in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(column)].width = 45 if column == 1 else 22
    for row in ws:
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = f"A{header_row + 1}"
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{ws.max_row}"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    if logo:
        from openpyxl.drawing.image import Image
        picture = Image(BytesIO(base64.b64decode(logo.split(",", 1)[1])))
        ratio = min(140 / picture.width, 45 / picture.height)
        picture.width *= ratio
        picture.height *= ratio
        ws.add_image(picture, "D1")
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def read_materials(raw):
    from openpyxl import load_workbook
    if len(raw) > 5 * 1024 * 1024:
        fail("Файл должен быть не больше 5 МБ")
    try:
        with ZipFile(BytesIO(raw)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 25 * 1024 * 1024:
                fail("Слишком большой распакованный файл")
        wb = load_workbook(BytesIO(raw), read_only=True, data_only=False)
    except (BadZipFile, ValueError, OSError, KeyError):
        fail("Нужен корректный файл XLSX")
    try:
        sheet = wb.active
        iterator = sheet.iter_rows(max_row=1002, max_col=6, values_only=True)
        headers = next(iterator, ())
        expected = ("Наименование", "Категория", "Единица", "Цена", "Действует с", "Источник")
        if tuple(headers) != expected:
            fail("Используйте выгрузку справочника: Наименование, Категория, Единица, Цена, Действует с, Источник")
        result = []
        for index, row in enumerate(iterator, start=2):
            if all(v is None for v in row):
                continue
            if index > 1001:
                fail("За один раз можно импортировать до 1000 материалов")
            if any(isinstance(v, str) and v.startswith("=") for v in row):
                fail("В импортируемом файле не должно быть формул")
            effective = row[4].date().isoformat() if hasattr(row[4], "date") else str(row[4] or "")
            result.append({"name": str(row[0] or ""), "category": str(row[1] or ""), "unit": str(row[2] or ""),
                           "price": str(row[3]) if row[3] is not None else None, "effective_on": effective, "price_source": str(row[5] or "")})
        if not result:
            fail("Файл не содержит материалов")
        return result
    finally:
        wb.close()


def estimate_document(data, format, internal=False, no_prices=False):
    if internal:
        headers = ["Материал", "Категория", "Единица", "Количество"]
        rows = [[l["name"], l["category"], l["unit"], l["quantity"]] for l in data["bom"]]
        if not no_prices:
            headers += ["Цена закупки", "Сумма"]
            rows = [row + [line["price"], line["amount"]] for row, line in zip(rows, data["bom"])]
    else:
        headers = ["Изделие / опция", "Пакет", "Количество"]
        rows = [[l["title"], l["package"], l["quantity"]] for l in data["commercial"]]
        if not no_prices:
            headers += ["Цена", "Сумма"]
            rows = [row + [line["unit_price"], line["amount"]] for row, line in zip(rows, data["commercial"])]
    title = f'{data["number"] or "Смета " + str(data["id"])} · {data["title"]} · версия {data["revision"]}'
    if not no_prices:
        rows += [["Сумма по прайсу", *[""] * (len(headers) - 2), data["list_price"]],
                 ["Скидка / изменение цены", *[""] * (len(headers) - 2), data["discount_amount"]],
                 ["Итого, ₽", *[""] * (len(headers) - 2), data["price"]]]
        if internal:
            rows += [["Себестоимость, ₽", *[""] * (len(headers) - 2), data["result"]["cost"]],
                     ["Маржа, %", *[""] * (len(headers) - 2), data["result"]["margin_percent"]]]
    if format == "xlsx":
        return workbook(headers, rows, "Смета", data["company"], data["logo"]), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    head = "".join("<th>" + escape(h) + "</th>" for h in headers)
    body = "".join("<tr>" + "".join("<td>" + escape(str(v) if v is not None else "—") + "</td>" for v in row) + "</tr>" for row in rows)
    css = "body{font:11px sans-serif;color:#173c33}h1{font-size:18px}table{width:100%;border-collapse:collapse}th,td{border:1px solid #ddd;padding:7px;text-align:left}th{background:#edf4f1}p{white-space:pre-wrap}"
    html = f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body><h1>{escape(data['company'])}</h1><h2>{escape(title)}</h2><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table><p>{escape(data['note'])}</p></body></html>"
    if format == "html":
        if data["logo"]:
            html = html.replace("<h1>", '<img style="max-width:160px;max-height:50px" src="' + data["logo"] + '"><h1>', 1)
        return html.encode("utf-8"), "text/html; charset=utf-8"
    if format == "pdf":
        import fitz
        story = fitz.Story(html=html, user_css=css)
        def rectangle(index, filled):
            if index >= 200:
                fail("Смета слишком велика для PDF")
            return fitz.Rect(0, 0, 595, 842), fitz.Rect(36, 85 if index == 0 and data["logo"] else 36, 559, 806), None
        document = story.write_with_links(rectangle)
        try:
            if data["logo"]:
                document[0].insert_image(fitz.Rect(36, 25, 190, 70), stream=base64.b64decode(data["logo"].split(",", 1)[1]))
            return document.tobytes(deflate=True), "application/pdf"
        finally:
            document.close()
    fail("Доступны XLSX, PDF и печатный HTML", 404)
