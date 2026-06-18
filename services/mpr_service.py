import csv
import re
from copy import deepcopy
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from openpyxl import load_workbook

from config import MPR_TEMPLATES_ROOT


APPENDIX_PLACEHOLDER = "{{APPENDIX_1_TABLE}}"
MPR_TEMPLATE_FILENAME = "template.docx"
MPR_TEMPLATE_NAMES = {
    "os_update": "Обновление ОС",
}

REQUIRED_COLUMNS = [
    "Имя",
    "Наименование услуги",
    "Имя дата-центра ВМ",
    "Имя AC",
    "ID КЭ сервера",
    "Платформа",
    "Статус стенда",
]

SOURCE_COLUMNS = [
    "Имя",
    "Наименование услуги",
    "Имя дата-центра ВМ",
    "Имя AC",
    "ID КЭ сервера",
    "Платформа",
]

DOCX_HEADERS = ["№", "Имя AC", "Наименование услуги", "Имя", "ЦОД"]


class MprError(Exception):
    """User-facing MPR validation/generation error."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or []


def _normalize_header(value):
    return str(value or "").replace("\ufeff", "").strip()


def _normalize_cell(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _safe_template_code(value):
    code = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        return ""
    return code


def list_mpr_templates():
    MPR_TEMPLATES_ROOT.mkdir(parents=True, exist_ok=True)
    templates = []
    for folder in sorted(MPR_TEMPLATES_ROOT.iterdir()):
        if not folder.is_dir():
            continue
        path = folder / MPR_TEMPLATE_FILENAME
        if not path.exists() or path.name.startswith("~$"):
            continue
        code = folder.name
        templates.append({
            "code": code,
            "name": MPR_TEMPLATE_NAMES.get(code, code.replace("_", " ").strip() or code),
            "filename": f"{code}/{MPR_TEMPLATE_FILENAME}",
        })
    return templates


def resolve_mpr_template(template_code):
    code = _safe_template_code(template_code)
    if not code:
        raise MprError("Не выбран шаблон МПР")

    candidates = {item["code"]: item for item in list_mpr_templates()}
    if code not in candidates:
        raise MprError("Шаблон DOCX не найден")

    path = (MPR_TEMPLATES_ROOT / code / MPR_TEMPLATE_FILENAME).resolve()
    root = MPR_TEMPLATES_ROOT.resolve()
    if root not in path.parents or not path.exists():
        raise MprError("Шаблон DOCX не найден")
    return path, candidates[code]


def build_mpr_rows(files):
    if not files:
        raise MprError("Не загружены файлы")

    rows = []
    errors = []

    for uploaded in files:
        filename = uploaded.filename or "без имени"
        suffix = Path(filename).suffix.lower()
        if suffix not in {".xlsx", ".csv"}:
            errors.append(f"{filename}: неподдерживаемый формат файла")
            continue

        try:
            file_rows = _read_xlsx(uploaded, filename) if suffix == ".xlsx" else _read_csv(uploaded, filename)
            rows.extend(file_rows)
        except MprError as exc:
            errors.append(exc.message)

    if errors:
        raise MprError("Не удалось обработать загруженные файлы", errors)

    filtered = []
    for row in rows:
        if row.get("Статус стенда", "").strip().casefold() != "работает":
            continue
        filtered.append({column: row.get(column, "") for column in SOURCE_COLUMNS})

    if not filtered:
        raise MprError("После фильтрации нет строк со статусом «Работает»")

    deduped = []
    seen = set()
    for row in filtered:
        marker = tuple(row.get(column, "") for column in SOURCE_COLUMNS)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append({
            "КТС": row.get("Имя", ""),
            "Наименование": row.get("Наименование услуги", ""),
            "ЦОД": row.get("Имя дата-центра ВМ", ""),
            "Имя AC": row.get("Имя AC", ""),
            "ID КЭ сервера": row.get("ID КЭ сервера", ""),
            "Платформа": row.get("Платформа", ""),
        })

    return sorted(
        deduped,
        key=lambda item: (
            item.get("Имя AC", "").casefold(),
            item.get("Наименование", "").casefold(),
            item.get("КТС", "").casefold(),
        ),
    )


def _read_xlsx(uploaded, filename):
    data = BytesIO(uploaded.read())
    try:
        workbook = load_workbook(data, read_only=True, data_only=True)
    except Exception as exc:
        raise MprError(f"{filename}: не удалось прочитать XLSX") from exc

    if "История лимитов" not in workbook.sheetnames:
        raise MprError(f"{filename}: отсутствует лист «История лимитов»")

    sheet = workbook["История лимитов"]
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        headers = [_normalize_header(value) for value in next(rows_iter)]
    except StopIteration as exc:
        raise MprError(f"{filename}: лист «История лимитов» пуст") from exc

    _validate_columns(headers, filename)
    indexes = {header: headers.index(header) for header in REQUIRED_COLUMNS}
    result = []
    for values in rows_iter:
        row = {}
        for column in REQUIRED_COLUMNS:
            index = indexes[column]
            row[column] = _normalize_cell(values[index] if index < len(values) else "")
        result.append(row)
    return result


def _read_csv(uploaded, filename):
    raw = uploaded.read()
    text = None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise MprError(f"{filename}: не удалось определить кодировку CSV")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t,")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"

    reader = csv.DictReader(StringIO(text), dialect=dialect)
    headers = [_normalize_header(header) for header in (reader.fieldnames or [])]
    _validate_columns(headers, filename)

    result = []
    for raw_row in reader:
        normalized = {_normalize_header(key): _normalize_cell(value) for key, value in raw_row.items()}
        result.append({column: normalized.get(column, "") for column in REQUIRED_COLUMNS})
    return result


def _validate_columns(headers, filename):
    present = set(headers)
    missing = [column for column in REQUIRED_COLUMNS if column not in present]
    if missing:
        raise MprError(f"{filename}: отсутствуют обязательные колонки: {', '.join(missing)}")


def generate_mpr_docx(template_path, rows):
    try:
        document = Document(template_path)
    except Exception as exc:
        raise MprError("Не удалось открыть DOCX-шаблон") from exc

    paragraph = _find_placeholder_paragraph(document)
    if paragraph is None:
        raise MprError(f"Плейсхолдер {APPENDIX_PLACEHOLDER} не найден в DOCX-шаблоне")

    table = _build_appendix_table(document, rows)
    _insert_table_at_placeholder(paragraph, table)

    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


def build_output_filename(template_info):
    name = template_info.get("name") or template_info.get("code") or "МПР"
    safe_name = re.sub(r'[<>:"/\\|?*]+', " ", name).strip() or "МПР"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"МПР_{safe_name}_{timestamp}.docx"


def _find_placeholder_paragraph(document):
    for paragraph in document.paragraphs:
        if APPENDIX_PLACEHOLDER in paragraph.text:
            return paragraph
    return None


def _build_appendix_table(document, rows):
    table = document.add_table(rows=1, cols=len(DOCX_HEADERS))
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    try:
        table.style = "Table Grid"
    except Exception:
        pass

    try:
        normal_style = document.styles["Normal"]
    except Exception:
        normal_style = None
    font_name = normal_style.font.name if normal_style and normal_style.font.name else None
    font_size = normal_style.font.size if normal_style and normal_style.font.size else Pt(9)

    header_cells = table.rows[0].cells
    for index, header in enumerate(DOCX_HEADERS):
        _set_cell_text(header_cells[index], header, bold=True, font_name=font_name, font_size=font_size)
        _shade_cell(header_cells[index], "EAF2F8")

    for number, item in enumerate(rows, start=1):
        cells = table.add_row().cells
        values = [
            str(number),
            item.get("Имя AC", ""),
            item.get("Наименование", ""),
            item.get("КТС", ""),
            item.get("ЦОД", ""),
        ]
        for index, value in enumerate(values):
            _set_cell_text(cells[index], value, font_name=font_name, font_size=font_size)

    for row in table.rows:
        for cell in row.cells:
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            _set_cell_margins(cell, top=80, start=100, bottom=80, end=100)

    _apply_table_geometry(document, table)
    _repeat_header_row(table.rows[0])
    return table


def _insert_table_at_placeholder(paragraph, table):
    remaining_text = paragraph.text.replace(APPENDIX_PLACEHOLDER, "").strip()
    paragraph._p.addnext(table._tbl)
    if remaining_text:
        _replace_paragraph_text(paragraph, remaining_text)
    else:
        parent = paragraph._element.getparent()
        parent.remove(paragraph._element)


def _replace_paragraph_text(paragraph, text):
    for run in paragraph.runs:
        run.text = ""
    if paragraph.runs:
        paragraph.runs[0].text = text
    else:
        paragraph.add_run(text)


def _set_cell_text(cell, text, bold=False, font_name=None, font_size=None):
    cell.text = ""
    paragraph = cell.paragraphs[0]
    run = paragraph.add_run(str(text or ""))
    run.bold = bold
    if font_name:
        run.font.name = font_name
        run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
    if font_size:
        run.font.size = font_size


def _shade_cell(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _set_cell_margins(cell, top=80, start=100, bottom=80, end=100):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for margin, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{margin}"))
        if node is None:
            node = OxmlElement(f"w:{margin}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _apply_table_geometry(document, table):
    section = document.sections[0]
    total_width = section.page_width.twips - section.left_margin.twips - section.right_margin.twips
    weights = [0.07, 0.24, 0.34, 0.22, 0.13]
    widths = [int(total_width * weight) for weight in weights]
    widths[-1] = int(total_width) - sum(widths[:-1])

    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(int(total_width)))
    tbl_w.set(qn("w:type"), "dxa")

    tbl_grid = table._tbl.tblGrid
    if tbl_grid is not None:
        table._tbl.remove(tbl_grid)
    tbl_grid = OxmlElement("w:tblGrid")
    table._tbl.insert(table._tbl.index(tbl_pr) + 1, tbl_grid)
    for width in widths:
        grid_col = OxmlElement("w:gridCol")
        grid_col.set(qn("w:w"), str(width))
        tbl_grid.append(grid_col)

    for row in table.rows:
        for index, cell in enumerate(row.cells):
            cell.width = widths[index]
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_w = tc_pr.find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                tc_pr.append(tc_w)
            tc_w.set(qn("w:w"), str(widths[index]))
            tc_w.set(qn("w:type"), "dxa")


def _repeat_header_row(row):
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)
