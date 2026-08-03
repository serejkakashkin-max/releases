from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from docx import Document
from lxml import etree


MAX_ENTRIES = 2048
MAX_UNCOMPRESSED = 100 * 1024 * 1024
MAX_ENTRY = 32 * 1024 * 1024
MAX_RATIO = 100
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REQUIRED_PARTS = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
LEGACY_PLACEHOLDERS = (
    "RELEASE_VERSION", "PREV_VERSION", "RELEASE_ID", "OPLOT", "CHECKER",
    "DATE", "PLUS_1", "PLAYBOOKS", "INSTRUCTION_BLOCK", "ИНСТРУКЦИЯ", "POB", "RELNUMBER",
)
PLACEHOLDER_RE = re.compile(
    r"\{\{[^{}]+\}\}|\$\{[^{}]+\}|<<[^<>]+>>|\[\[[^\[\]]+\]\]|\b(?:"
    + "|".join(map(re.escape, LEGACY_PLACEHOLDERS)) + r")\b"
)
FORBIDDEN_PREFIXES = (
    "word/activeX/", "word/embeddings/", "word/vbaProject", "word/oleObject",
)
FORBIDDEN_SUFFIXES = (".exe", ".dll", ".com", ".bat", ".cmd", ".js", ".vbs", ".ps1", ".msi", ".scr")


@dataclass(frozen=True)
class ValidationFailure:
    code: str
    message: str
    group: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "group": self.group}


def _unsafe_zip_name(name: str) -> bool:
    normalized = unicodedata.normalize("NFC", name)
    if not normalized or "\0" in normalized or "\\" in normalized or re.match(r"^[A-Za-z]:", normalized) or normalized.startswith("/"):
        return True
    return any(part in {"", ".", ".."} for part in PurePosixPath(normalized).parts)


def _relationship_files(archive: zipfile.ZipFile) -> list[str]:
    return [name for name in archive.namelist() if name.casefold().endswith(".rels")]


def _unique_failures(errors: list[ValidationFailure]) -> list[ValidationFailure]:
    unique = {(item.code, item.message, item.group): item for item in errors}
    return list(unique.values())


def _read_bounded(archive: zipfile.ZipFile, name: str, *, limit: int = MAX_ENTRY) -> bytes:
    """Read one already-vetted ZIP member without trusting its declared size."""
    payload = bytearray()
    with archive.open(name, "r") as source:
        remaining = limit + 1
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            payload.extend(chunk)
            remaining -= len(chunk)
    if len(payload) > limit:
        raise ValueError("zip_entry_too_large")
    return bytes(payload)


def _safe_xml(payload: bytes) -> etree._Element:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.I):
        raise ValueError("xml_dtd")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False, huge_tree=False)
    return etree.fromstring(payload, parser=parser)


def _owner_for_relationship(path: str) -> str:
    if path == "_rels/.rels":
        return ""
    match = re.match(r"^(.*?)/_rels/([^/]+)\.rels$", path)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def inspect_docx(path: Path) -> tuple[list[ValidationFailure], set[str]]:
    errors: list[ValidationFailure] = []
    external_images: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > MAX_ENTRIES:
                errors.append(ValidationFailure("zip_too_many_entries", "В документе слишком много внутренних частей.", "security"))
            seen: set[str] = set()
            total = 0
            total_compressed = 0
            for info in infos:
                key = unicodedata.normalize("NFC", info.filename).casefold()
                if key in seen:
                    errors.append(ValidationFailure("zip_duplicate_name", "DOCX содержит конфликтующие имена внутренних частей.", "security"))
                seen.add(key)
                if _unsafe_zip_name(info.filename):
                    errors.append(ValidationFailure("zip_unsafe_path", "DOCX содержит небезопасный внутренний путь.", "security"))
                total += info.file_size
                total_compressed += info.compress_size
                if info.flag_bits & 1:
                    errors.append(ValidationFailure("zip_encrypted", "Зашифрованные DOCX не поддерживаются.", "security"))
                if info.file_size > MAX_ENTRY:
                    errors.append(ValidationFailure("zip_entry_too_large", "Одна из внутренних частей DOCX слишком велика.", "security"))
                if info.compress_size == 0 and info.file_size > 0 or info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                    errors.append(ValidationFailure("zip_compression_ratio", "DOCX имеет небезопасную степень сжатия.", "security"))
                lower = info.filename.casefold()
                if lower.startswith(tuple(item.casefold() for item in FORBIDDEN_PREFIXES)) or lower.endswith(FORBIDDEN_SUFFIXES):
                    errors.append(ValidationFailure("forbidden_embedded_content", "Документ содержит неподдерживаемое встроенное содержимое.", "security"))
            if total > MAX_UNCOMPRESSED:
                errors.append(ValidationFailure("zip_uncompressed_too_large", "Распакованный DOCX превышает безопасный размер.", "security"))
            if total > 0 and (total_compressed == 0 or total / total_compressed > MAX_RATIO):
                errors.append(ValidationFailure("zip_overall_compression_ratio", "DOCX имеет небезопасную общую степень сжатия.", "security"))
            if not REQUIRED_PARTS.issubset(set(names)):
                errors.append(ValidationFailure("ooxml_required_parts", "Файл не является полноценным документом Word.", "structure"))

            # Phase A is central-directory-only. Never decompress any member when a
            # size/path/encryption/structure gate has already failed.
            if errors:
                return _unique_failures(errors), external_images

            for rel_path in _relationship_files(archive):
                try:
                    root = _safe_xml(_read_bounded(archive, rel_path))
                except (KeyError, etree.XMLSyntaxError, ValueError):
                    errors.append(ValidationFailure("relationship_xml_invalid", "Связи DOCX повреждены или небезопасны.", "security"))
                    continue
                owner = _owner_for_relationship(rel_path)
                owner_dir = posixpath.dirname(owner)
                for rel in root.xpath("//*[local-name()='Relationship']"):
                    target = str(rel.get("Target") or "")
                    relation_type = str(rel.get("Type") or "").casefold()
                    external = str(rel.get("TargetMode") or "").casefold() == "external"
                    if external:
                        scheme = urlsplit(target).scheme.casefold()
                        if relation_type.endswith("/image"):
                            if scheme not in {"http", "https"}:
                                errors.append(ValidationFailure("external_image_scheme", "Внешнее изображение использует небезопасный адрес.", "security"))
                            external_images.add(unicodedata.normalize("NFC", target))
                        elif relation_type.endswith("/hyperlink") and scheme in {"http", "https", "mailto"}:
                            pass
                        elif relation_type.endswith("/attachedtemplate"):
                            errors.append(ValidationFailure("external_attached_template", "Внешний подключённый шаблон запрещён.", "security"))
                        else:
                            errors.append(ValidationFailure("external_relationship", "DOCX содержит запрещённую внешнюю связь.", "security"))
                    else:
                        decoded_target = unquote(target)
                        resolved = posixpath.normpath(posixpath.join(owner_dir, decoded_target))
                        if "\\" in decoded_target or re.match(r"^[A-Za-z]:", decoded_target) or decoded_target.startswith(("/", "//")) or resolved.startswith("../") or resolved == "..":
                            errors.append(ValidationFailure("relationship_escape", "Внутренняя связь DOCX выходит за границы файла.", "security"))
                        if relation_type.endswith("/afchunk") or relation_type.endswith("/altchunk"):
                            errors.append(ValidationFailure("altchunk", "Встроенные внешние фрагменты не поддерживаются.", "security"))
            for name in names:
                if name.casefold().endswith(".xml") or name.casefold().endswith(".rels"):
                    try:
                        _safe_xml(_read_bounded(archive, name))
                    except (etree.XMLSyntaxError, ValueError):
                        errors.append(ValidationFailure("xml_unsafe", "Одна из XML-частей DOCX повреждена или небезопасна.", "security"))
                        break
    except (zipfile.BadZipFile, OSError):
        errors.append(ValidationFailure("not_docx", "Выбранный файл не является корректным DOCX.", "structure"))
    if not errors:
        try:
            Document(path)
        except Exception:
            errors.append(ValidationFailure("python_docx_reopen", "Документ не открывается стандартным обработчиком Word.", "structure"))
    return _unique_failures(errors), external_images


def _damaged_recognized_placeholders(text: str) -> set[str]:
    damaged: set[str] = set()
    for match in re.finditer(r"[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]*(?:(?:\s+_?|_\s+)[A-ZА-ЯЁ0-9]+)+", text):
        raw = match.group(0)
        canonical = re.sub(r"\s+", "", raw)
        if canonical in LEGACY_PLACEHOLDERS and raw != canonical:
            damaged.add(canonical)
    return damaged


def _paragraph_placeholders(document: Document) -> tuple[Counter, dict[str, Counter], set[str]]:
    total: Counter = Counter()
    areas: dict[str, Counter] = {"body": Counter(), "table": Counter(), "unsupported": Counter()}
    damaged: set[str] = set()
    for paragraph in document.paragraphs:
        values = PLACEHOLDER_RE.findall(paragraph.text)
        total.update(values); areas["body"].update(values)
        damaged.update(_damaged_recognized_placeholders(paragraph.text))
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    values = PLACEHOLDER_RE.findall(paragraph.text)
                    total.update(values); areas["table"].update(values)
                    damaged.update(_damaged_recognized_placeholders(paragraph.text))
    for section in document.sections:
        for story in (section.header, section.footer):
            for paragraph in story.paragraphs:
                values = PLACEHOLDER_RE.findall(paragraph.text)
                total.update(values); areas["unsupported"].update(values)
                damaged.update(_damaged_recognized_placeholders(paragraph.text))
    return total, areas, damaged


def _jira_table_count(document: Document) -> tuple[int, list[ValidationFailure]]:
    count = 0
    errors: list[ValidationFailure] = []
    for table in document.tables:
        if not table.rows:
            continue
        headers = [cell.text.strip() for cell in table.rows[0].cells]
        if "ЗНИ/JIRA ID" in headers:
            count += 1
            folded = {item.casefold() for item in headers}
            if len(headers) < 4 or not folded.intersection({"issue type", "issue", "type"}):
                errors.append(ValidationFailure("jira_table_columns", "Таблица Jira должна содержать не менее четырёх колонок и колонку типа задачи.", "jira"))
    return count, errors


def build_contract(path: Path) -> dict[str, Any]:
    document = Document(path)
    total, areas, damaged = _paragraph_placeholders(document)
    try:
        with zipfile.ZipFile(path) as archive:
            for part_name in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
                if part_name not in archive.namelist():
                    continue
                xml_root = _safe_xml(_read_bounded(archive, part_name))
                if part_name == "word/document.xml":
                    nodes = xml_root.xpath("//*[local-name()='txbxContent']")
                else:
                    nodes = [xml_root]
                unsupported_values = []
                for node in nodes:
                    logical_text = "".join(node.itertext())
                    unsupported_values.extend(PLACEHOLDER_RE.findall(logical_text))
                    damaged.update(_damaged_recognized_placeholders(logical_text))
                if unsupported_values:
                    total.update(unsupported_values); areas["unsupported"].update(unsupported_values)
    except (OSError, KeyError, zipfile.BadZipFile, etree.XMLSyntaxError, ValueError):
        pass
    jira_count, jira_errors = _jira_table_count(document)
    return {
        "placeholders": dict(total),
        "areas": {key: dict(value) for key, value in areas.items()},
        "damaged_placeholders": sorted(damaged),
        "jira_table_count": jira_count,
        "jira_errors": [item.as_dict() for item in jira_errors],
    }


def compare_contract(active_path: Path, candidate_path: Path) -> tuple[list[ValidationFailure], dict[str, Any]]:
    active = build_contract(active_path)
    candidate = build_contract(candidate_path)
    errors: list[ValidationFailure] = []
    if active["placeholders"] != candidate["placeholders"]:
        errors.append(ValidationFailure("placeholder_multiset", "Служебные поля должны совпадать с действующим шаблоном.", "placeholders"))
    if active["areas"] != candidate["areas"]:
        errors.append(ValidationFailure("placeholder_area", "Служебное поле перемещено в неподдерживаемую область документа.", "placeholders"))
    if candidate["areas"].get("unsupported"):
        errors.append(ValidationFailure("placeholder_unsupported_area", "Служебные поля в колонтитулах и специальных областях не поддерживаются.", "placeholders"))
    if candidate.get("damaged_placeholders"):
        errors.append(ValidationFailure("placeholder_whitespace_damaged", "Распознанное служебное поле повреждено пробелами.", "placeholders"))
    if active["jira_table_count"] != candidate["jira_table_count"]:
        errors.append(ValidationFailure("jira_table_count", "Количество Jira-таблиц должно совпадать с действующим шаблоном.", "jira"))
    errors.extend(ValidationFailure(**item) for item in candidate["jira_errors"])
    return errors, candidate


def validate_candidate(active_path: Path, candidate_path: Path) -> dict[str, Any]:
    active_errors, active_external_images = inspect_docx(active_path)
    candidate_errors, candidate_external_images = inspect_docx(candidate_path)
    errors = list(candidate_errors)
    if active_errors:
        errors.append(ValidationFailure("active_template_unavailable", "Действующий шаблон не прошёл контрольное чтение.", "structure"))
    added_images = candidate_external_images - active_external_images
    warnings: list[ValidationFailure] = []
    if added_images:
        errors.append(ValidationFailure("new_external_image", "Новая версия добавляет внешние изображения.", "security"))
    elif candidate_external_images:
        warnings.append(ValidationFailure("grandfathered_external_image", "Сохранены ранее существовавшие внешние изображения; они не загружаются автоматически.", "security"))
    contract: dict[str, Any] = {}
    if not errors:
        try:
            contract_errors, contract = compare_contract(active_path, candidate_path)
            errors.extend(contract_errors)
        except Exception:
            errors.append(ValidationFailure("contract_read", "Не удалось проверить структуру служебных полей.", "structure"))
    return {
        "ok": not errors,
        "errors": [item.as_dict() for item in errors],
        "warnings": [item.as_dict() for item in warnings],
        "contract": contract,
        "checks": {
            "security": not any(item.group == "security" for item in errors),
            "structure": not any(item.group == "structure" for item in errors),
            "placeholders": not any(item.group == "placeholders" for item in errors),
            "jira": not any(item.group == "jira" for item in errors),
        },
    }
