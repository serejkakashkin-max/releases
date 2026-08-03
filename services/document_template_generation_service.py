from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from docx import Document

from services.docx_service import replace_keys_in_doc
from services.document_template_runtime_service import atomic_write_bytes
from services.document_template_storage_service import candidate_directory
from services.document_template_validation_service import PLACEHOLDER_RE, inspect_docx


SYNTHETIC_JIRA_BASE = "https://example.invalid/jira"


def _synthetic_context(document: Document) -> dict[str, str]:
    keys: set[str] = set()
    paragraphs = list(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    for paragraph in paragraphs:
        keys.update(PLACEHOLDER_RE.findall(paragraph.text))
    today = datetime(2030, 1, 15)
    conventional = {
        "RELEASE_VERSION": "99.99.99",
        "PREV_VERSION": "99.99.98",
        "RELEASE_ID": "SYNTHETIC-1",
        "OPLOT": "Тестовый редактор Oplot",
        "CHECKER": "Тестовый проверяющий",
        "DATE": today.strftime("%d.%m.%Y"),
        "PLUS_1": (today + timedelta(days=1)).strftime("%d.%m.%Y"),
        "PLAYBOOKS": "SYNTHETIC_PLAYBOOK",
        "INSTRUCTION_BLOCK": "Отсутствуют",
        "POB": "Синтетический ПОВ",
        "RELNUMBER": "SYNTHETIC-1",
    }
    context = dict(conventional)
    for key in keys:
        context.setdefault(key, f"Тестовое значение {key[:40]}")
    return context


def generate_synthetic_document(candidate_path: Path, output: Path) -> tuple[Path | None, list[dict[str, str]]]:
    try:
        document = Document(candidate_path)
        context = _synthetic_context(document)
        issue = {"key": "SYNTHETIC-1", "summary": "Синтетическая задача без сетевых запросов", "type": "Task"}
        generated = replace_keys_in_doc(
            document,
            context,
            [issue],
            "SYNTHETIC-1",
            jira_base_url=SYNTHETIC_JIRA_BASE,
        )
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.parent / f".{output.name}.generating"
        generated.save(temporary)
        payload = temporary.read_bytes()
        temporary.unlink(missing_ok=True)
        atomic_write_bytes(output, payload)
        errors, _ = inspect_docx(output)
        if errors:
            output.unlink(missing_ok=True)
            return None, [{"code": "generation_security", "message": "Тестовый документ не прошёл контрольное открытие.", "group": "generation"}]
        reopened = Document(output)
        unresolved = []
        for paragraph in reopened.paragraphs:
            unresolved.extend(PLACEHOLDER_RE.findall(paragraph.text))
        if unresolved:
            output.unlink(missing_ok=True)
            return None, [{"code": "generation_placeholders", "message": "В тестовом документе остались незаполненные служебные поля.", "group": "generation"}]
        return output, []
    except Exception:
        return None, [{"code": "generation_failed", "message": "Не удалось создать тестовый документ.", "group": "generation"}]


def generate_test_document(candidate_uuid: str, candidate_path: Path) -> tuple[Path | None, list[dict[str, str]]]:
    return generate_synthetic_document(candidate_path, candidate_directory(candidate_uuid) / "test.docx")
