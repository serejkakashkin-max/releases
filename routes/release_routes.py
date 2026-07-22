import os
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, request, send_file, jsonify
from zipfile import ZipFile
from io import BytesIO
import tempfile
from collections import defaultdict
from docx import Document  # ДОБАВЛЕНО

from extensions import RELEASE_STRUCTURE, ID_MAP  # Импортируем из extensions
from config import DOC_TEMPLATES_ROOT, DEFAULT_BH_PLAYBOOKS
from services.jira_service import (
    extract_sm_id_and_summary,
    get_release_jira_snapshot,
)
from services.release_monitor_service import (
    get_release_monitor_snapshot,
    normalize_release_type,
    sync_release_monitor_jira_fields,
)
from services.docx_service import replace_keys_in_doc
from services.counter_service import increment_counter  # НОВОЕ: импорт счетчика
from services.release_template_catalog_service import (
    build_runtime_template_catalog,
    find_template_entries_by_ke,
    is_ai_agents_template_category,
    select_template_by_summary,
    template_requires_playbooks,
)

BASE_PATH = os.getenv("BASE_PATH", "")

release_bp = Blueprint('release', __name__)

# УБРАНО: определение get_release_structure() - оно теперь в extensions.py


def release_uses_playbooks(release_name: str, category: str = "", *, catalog_entries=None) -> bool:
    """Определяет необходимость плейбуков по каталогу или старому fallback-правилу."""
    if is_ai_agents_template_category(category):
        return False

    if catalog_entries is None:
        catalog_value = template_requires_playbooks(release_full=release_name, category=category)
    else:
        catalog_value = None
        for entry in catalog_entries:
            if release_name and entry.get("release_full") != release_name:
                continue
            if category and entry.get("category") != category:
                continue
            if entry.get("requires_playbooks") is not None:
                catalog_value = bool(entry.get("requires_playbooks"))
            break
    if catalog_value is not None:
        return catalog_value

    release_name_upper = (release_name or "").upper()
    blocked_markers = ("SOWA", "ЕФС.AUTHENTICATION_USER", "AUTH", "RESSTORE(2889318)")
    return not any(marker in release_name_upper for marker in blocked_markers)


def _catalog_template_payload(candidate: dict) -> dict:
    return {
        "found": True,
        "category": candidate["category"],
        "release_clean": candidate["release_clean"],
        "release_full": candidate["release_full"],
        "variant": candidate.get("variant", ""),
        "requires_playbooks": candidate.get("requires_playbooks"),
        "candidates": None,
    }


def _legacy_template_payload(
    category: str,
    release_clean: str,
    release_full: str,
    *,
    catalog_entries=None,
) -> dict:
    return {
        "found": True,
        "category": category,
        "release_clean": release_clean,
        "release_full": release_full,
        "requires_playbooks": release_uses_playbooks(
            release_full,
            category,
            catalog_entries=catalog_entries,
        ),
        "candidates": None,
    }


def build_release_template_detection_context() -> dict:
    entries = build_runtime_template_catalog()
    by_ke = defaultdict(list)
    for entry in entries:
        ke = str(entry.get("ke") or "").strip()
        if ke:
            by_ke[ke].append(entry)
    return {
        "entries": entries,
        "by_ke": dict(by_ke),
    }


def detect_release_template_from_values(sm_id: str, summary: str = "", *, catalog_context=None):
    """Определяет шаблон по уже известным КЭ релиза и summary без запроса в Jira."""
    sm_id = (sm_id or "").strip()
    summary = summary or ""
    result = {"found": False, "candidates": [], "template_sm_id": sm_id}

    if catalog_context is None:
        catalog_candidates = find_template_entries_by_ke(sm_id) if sm_id else []
        catalog_entries = None
    else:
        catalog_candidates = list((catalog_context.get("by_ke") or {}).get(sm_id) or [])
        catalog_entries = list(catalog_context.get("entries") or [])
    if catalog_candidates:
        if len(catalog_candidates) == 1:
            return {**_catalog_template_payload(catalog_candidates[0]), "template_sm_id": sm_id}

        selected = select_template_by_summary(catalog_candidates, summary)
        if selected:
            return {**_catalog_template_payload(selected), "template_sm_id": sm_id}

        return {
            "found": False,
            "template_sm_id": sm_id,
            "candidates": [
                {
                    "category": candidate["category"],
                    "release_clean": candidate["release_clean"],
                    "release_full": candidate["release_full"],
                    "variant": candidate.get("variant", ""),
                    "requires_playbooks": candidate.get("requires_playbooks"),
                }
                for candidate in catalog_candidates
            ],
        }

    if sm_id and sm_id in ID_MAP:
        candidates = ID_MAP[sm_id]
        if len(candidates) == 1:
            category, release_name_clean = candidates[0]
            for clean, full in RELEASE_STRUCTURE.get(category, []):
                if clean == release_name_clean:
                    return {
                        **_legacy_template_payload(
                            category,
                            release_name_clean,
                            full,
                            catalog_entries=catalog_entries,
                        ),
                        "template_sm_id": sm_id,
                    }
        else:
            summary_lower = summary.lower() if summary else ""
            selected = None
            for cand_category, cand_release_clean in candidates:
                cand_lower = cand_release_clean.lower()
                if "blue" in summary_lower and "blue" in cand_lower:
                    selected = (cand_category, cand_release_clean)
                    break
                elif "green" in summary_lower and "green" in cand_lower:
                    selected = (cand_category, cand_release_clean)
                    break
                elif "bh" in summary_lower and "bh" in cand_lower:
                    selected = (cand_category, cand_release_clean)
                    break
                elif "pl" in summary_lower and "pl" in cand_lower:
                    selected = (cand_category, cand_release_clean)
                    break
            if selected:
                category, release_name_clean = selected
                for clean, full in RELEASE_STRUCTURE.get(category, []):
                    if clean == release_name_clean:
                        return {
                            **_legacy_template_payload(
                                category,
                                release_name_clean,
                                full,
                                catalog_entries=catalog_entries,
                            ),
                            "template_sm_id": sm_id,
                        }

            candidates_list = []
            for cand_category, cand_release_clean in candidates:
                for clean, full in RELEASE_STRUCTURE.get(cand_category, []):
                    if clean == cand_release_clean:
                        candidates_list.append({
                            "category": cand_category,
                            "release_clean": cand_release_clean,
                            "release_full": full,
                            "requires_playbooks": release_uses_playbooks(full, cand_category),
                        })
                        break
            return {"found": False, "template_sm_id": sm_id, "candidates": candidates_list}

    return result


def detect_release_template(release_id: str, jira_snapshot: dict = None):
    release_id = (release_id or "").strip()
    if not release_id:
        return {"found": False, "candidates": [], "error": "No release_id provided"}

    if jira_snapshot is not None:
        sm_id = jira_snapshot.get("template_sm_id")
        summary = jira_snapshot.get("summary") or ""
    else:
        sm_id, summary = extract_sm_id_and_summary(release_id)
    return detect_release_template_from_values(sm_id, summary)


def _safe_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _normalize_monitor_text(value):
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _get_constructor_rollback_group(item):
    if str(item.get("ke_id") or "").strip() != "3894421":
        return ""

    text_parts = [
        item.get("release_summary", ""),
        item.get("ke_name", ""),
        item.get("release_version", ""),
    ]
    text_parts.extend(item.get("release_name_lines") or [])
    searchable = _normalize_monitor_text(" ".join(str(part or "") for part in text_parts))
    searchable = re.sub(r"[^0-9a-zа-яё]+", " ", searchable, flags=re.IGNORECASE)

    has_bh = bool(re.search(r"(^|\s)bh(\s|$)", searchable))
    has_pl = bool(re.search(r"(^|\s)pl(\s|$)", searchable))
    if has_bh and not has_pl:
        return "bh"
    if has_pl and not has_bh:
        return "pl"
    return ""


def _typed_lookup_key(value):
    try:
        hash(value)
    except TypeError:
        return type(value).__name__, repr(value)
    return "hashable", value


class PreviousReleaseVersionIndex:
    """Request-local index preserving the legacy previous-version contract."""

    def __init__(self, items):
        self._by_row_key = {}
        self._by_release_key = {}
        self._by_ke_exact_year = defaultdict(list)
        self._by_ke_numeric_year = defaultdict(list)
        self._by_exact_year = defaultdict(list)

        for item in items or []:
            row_key = str(item.get("row_key") or "").strip()
            release_key = str(item.get("release_key") or "").strip()
            if row_key:
                self._by_row_key.setdefault(row_key, item)
            if release_key:
                self._by_release_key.setdefault(release_key, item)

            release_number = _safe_int(item.get("release_number"))
            version = str(item.get("release_version") or "").strip()
            if release_number is None or not version:
                continue
            record = {
                "item": item,
                "number": release_number,
                "version": version,
                "release_key": release_key,
                "rollback_group": _get_constructor_rollback_group(item),
                "sort_key": (
                    release_number or -1,
                    release_key,
                    row_key,
                ),
            }
            ke_id = str(item.get("ke_id") or "").strip()
            exact_year = _typed_lookup_key(item.get("year"))
            numeric_year = _safe_int(item.get("year"))
            if ke_id:
                self._by_ke_exact_year[(ke_id, exact_year)].append(record)
                if numeric_year is not None:
                    self._by_ke_numeric_year[(ke_id, numeric_year)].append(record)
            self._by_exact_year[exact_year].append(record)

        for index in (
            self._by_ke_exact_year,
            self._by_ke_numeric_year,
            self._by_exact_year,
        ):
            for candidates in index.values():
                candidates.sort(key=lambda record: record["sort_key"])

    @staticmethod
    def _best_candidate(
        candidates,
        current_number,
        current_release_key,
        rollback_group,
        *,
        require_lower_number=True,
    ):
        for record in reversed(candidates or []):
            if require_lower_number and record["number"] >= current_number:
                continue
            if record["release_key"] == current_release_key:
                continue
            if rollback_group and record["rollback_group"] != rollback_group:
                continue
            return record["version"]
        return ""

    def resolve(self, row_key: str, release_id: str) -> str:
        normalized_row_key = str(row_key or "").strip()
        normalized_release_id = str(release_id or "").strip()
        current_item = self._by_row_key.get(normalized_row_key) if normalized_row_key else None
        if current_item is None and normalized_release_id:
            current_item = self._by_release_key.get(normalized_release_id)
        if current_item is None:
            return ""

        current_number = _safe_int(current_item.get("release_number"))
        current_release_key = str(
            current_item.get("release_key") or normalized_release_id or ""
        ).strip()
        current_ke_id = str(current_item.get("ke_id") or "").strip()
        current_year = current_item.get("year")
        release_type = normalize_release_type(current_item.get("release_type"))
        is_reroll = release_type == "reroll" if release_type else bool(current_item.get("is_reroll"))
        rollback_group = _get_constructor_rollback_group(current_item)

        if current_number is not None and current_ke_id:
            version = self._best_candidate(
                self._by_ke_exact_year.get((current_ke_id, _typed_lookup_key(current_year))),
                current_number,
                current_release_key,
                rollback_group,
            )
            if version:
                return version

            numeric_year = _safe_int(current_year)
            if numeric_year is not None:
                version = self._best_candidate(
                    self._by_ke_numeric_year.get((current_ke_id, numeric_year - 1)),
                    current_number,
                    current_release_key,
                    rollback_group,
                    require_lower_number=False,
                )
                if version:
                    return version

        if is_reroll and current_number is not None:
            return self._best_candidate(
                self._by_exact_year.get(_typed_lookup_key(current_year)),
                current_number,
                current_release_key,
                rollback_group,
            )
        return ""


def build_previous_release_version_index(items):
    return PreviousReleaseVersionIndex(items)


def get_previous_version_from_monitor_items(items, row_key: str, release_id: str):
    return build_previous_release_version_index(items).resolve(row_key, release_id)


def _get_previous_version_from_monitor_snapshot(row_key: str, release_id: str):
    snapshot = get_release_monitor_snapshot() or {}
    return get_previous_version_from_monitor_items(snapshot.get("items") or [], row_key, release_id)


def _normalize_release_date(raw_date: str):
    raw_date = (raw_date or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw_date, fmt)
            return parsed.strftime("%d.%m.%Y"), (parsed + timedelta(days=1)).strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise ValueError("Неверный формат даты")


def _generate_release_zip_buffer(
    *,
    category: str,
    release_full: str,
    release_id: str,
    release_version: str = "",
    prev_version: str,
    oplot: str,
    checker: str,
    instruction_link: str,
    date_str: str,
    ke: str,
    selected_playbooks,
    jira_snapshot: dict = None,
):
    if not category or not release_full:
        raise ValueError("Не выбраны категория и релиз. Используйте автоопределение или выберите вручную.")

    if not release_id:
        raise ValueError("Не указан номер релиза")

    t, tt = _normalize_release_date(date_str)
    template_dir = DOC_TEMPLATES_ROOT / category / release_full
    if not template_dir.exists():
        raise ValueError(f"Директория с шаблонами не найдена: {template_dir}")

    template_files = list(template_dir.glob("*.docx"))
    if not template_files:
        raise ValueError(f"Шаблоны не найдены в директории: {template_dir}")

    snapshot = jira_snapshot or get_release_jira_snapshot(release_id)
    release_version = (snapshot.get("release_version") or release_version or "").strip()
    ke = (snapshot.get("ke") or ke or "").strip()
    jira_issues = list(snapshot.get("issues") or [])
    instruction_block = "Выполнить пункты инструкции по внедрению ИНСТРУКЦИЯ" if instruction_link else "Отсутствуют"
    pob = snapshot.get("pob") or ""
    playbooks_text = "\n".join(selected_playbooks)

    context = {
        "RELEASE_VERSION": release_version,
        "release_version": release_version,
        "releases_version": release_version,
        "PREV_VERSION": prev_version,
        "RELEASE_ID": release_id,
        "OPLOT": oplot,
        "CHECKER": checker,
        "DATE": t,
        "PLUS_1": tt,
        "PLAYBOOKS": playbooks_text,
        "INSTRUCTION_BLOCK": instruction_block,
        "POB": pob,
        "RELNUMBER": release_id,
    }

    zip_buffer = BytesIO()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        generated_docs = []

        for path in template_files:
            doc = Document(path)
            doc = replace_keys_in_doc(
                doc,
                context,
                jira_issues,
                release_id,
                instruction_url=instruction_link if "План" in path.name else None,
            )
            stem = path.stem
            if ke:
                stem = stem.replace("КЭ", ke)

            output_path = temp_path / f"{stem}.docx"
            doc.save(output_path)
            generated_docs.append(output_path)

        with ZipFile(zip_buffer, "w") as zip_file:
            for doc_path in generated_docs:
                zip_file.write(doc_path, doc_path.name)

    zip_buffer.seek(0)
    return zip_buffer

@release_bp.route('/release/monitor-init', methods=['POST'])
def release_monitor_init():
    data = request.get_json(silent=True) or {}
    release_id = (data.get("release_id") or "").strip()
    row_key = (data.get("row_key") or "").strip()
    if not release_id:
        return jsonify({"success": False, "error": "Не указан номер релиза"}), 400

    started = datetime.now()
    jira_snapshot = get_release_jira_snapshot(release_id)
    detection = detect_release_template(release_id, jira_snapshot=jira_snapshot)
    if detection.get("error"):
        return jsonify({"success": False, "error": detection["error"]}), 400

    release_full = detection.get("release_full", "")
    playbooks_required = (
        detection.get("requires_playbooks")
        if isinstance(detection.get("requires_playbooks"), bool)
        else release_uses_playbooks(release_full, detection.get("category", ""))
        if detection.get("found")
        else None
    )
    jira_version = jira_snapshot.get("release_version") or ""
    jira_ke = jira_snapshot.get("ke") or ""
    incoming_ke = (data.get("ke") or "").strip()
    missing_distribution_fields = []
    if not jira_version:
        missing_distribution_fields.append("release_version")
    if not jira_ke:
        missing_distribution_fields.append("ke")
    sync_patch = {}
    try:
        sync_patch = sync_release_monitor_jira_fields(
            row_key=row_key,
            release_key=release_id,
            release_version=jira_version,
            ke=jira_ke,
        )
    except Exception as exc:
        logging.warning("Не удалось точечно обновить строку релиза из Jira: %s", exc)
    logging.debug(
        "Release monitor document init for %s completed in %.1f ms (jira_cache=%s)",
        release_id,
        (datetime.now() - started).total_seconds() * 1000,
        bool(jira_snapshot.get("from_cache")),
    )

    return jsonify({
        "success": True,
        "release_id": release_id,
        "detection": detection,
        "release_version": jira_version,
        "ke": (jira_ke or incoming_ke).strip(),
        "distribution_missing": bool(missing_distribution_fields),
        "missing_distribution_fields": missing_distribution_fields,
        "playbooks_required": playbooks_required,
        "playbooks": DEFAULT_BH_PLAYBOOKS,
        "oplot": (data.get("oplot") or "").strip(),
        "checker": (data.get("checker") or "").strip(),
        "date": (data.get("date") or "").strip(),
        "prev_version": _get_previous_version_from_monitor_snapshot(row_key, release_id),
        "sync_patch": sync_patch,
    })


@release_bp.route('/release/monitor-generate', methods=['POST'])
def release_monitor_generate():
    data = request.get_json(silent=True) or {}
    release_id = (data.get("release_id") or "").strip()
    release_version = (data.get("release_version") or "").strip()
    prev_version = (data.get("prev_version") or "").strip()
    oplot = (data.get("oplot") or "").strip()
    checker = (data.get("checker") or "").strip()
    instruction_link = (data.get("instruction_link") or "").strip()
    date_str = (data.get("date") or "").strip()
    ke = (data.get("ke") or "").strip()
    category = (data.get("category") or "").strip()
    release_full = (data.get("release_full") or "").strip()
    selected_playbooks = data.get("playbooks") or []

    if not release_id:
        return jsonify({"success": False, "error": "Не указан номер релиза"}), 400
    if not prev_version:
        return jsonify({"success": False, "error": "Не указана предыдущая версия"}), 400
    if not oplot:
        return jsonify({"success": False, "error": "Не назначен дежурный ОПЛОТ"}), 400
    if not checker:
        return jsonify({"success": False, "error": "Не указан проверяющий"}), 400
    if not date_str:
        return jsonify({"success": False, "error": "Не указана дата релиза"}), 400

    started = datetime.now()
    jira_snapshot = get_release_jira_snapshot(release_id)
    release_version = (jira_snapshot.get("release_version") or release_version or "").strip()
    ke = (jira_snapshot.get("ke") or ke or "").strip()

    if not category or not release_full:
        detection = detect_release_template(release_id, jira_snapshot=jira_snapshot)
        if not detection.get("found"):
            return jsonify({
                "success": False,
                "error": "Не удалось автоопределить шаблон релиза. Используйте стандартный генератор или выберите шаблон вручную.",
                "detection": detection,
            }), 400
        category = detection.get("category", "")
        release_full = detection.get("release_full", "")

    if not release_uses_playbooks(release_full, category):
        selected_playbooks = []

    try:
        zip_buffer = _generate_release_zip_buffer(
            category=category,
            release_full=release_full,
            release_id=release_id,
            release_version=release_version,
            prev_version=prev_version,
            oplot=oplot,
            checker=checker,
            instruction_link=instruction_link,
            date_str=date_str,
            ke=ke,
            selected_playbooks=selected_playbooks,
            jira_snapshot=jira_snapshot,
        )
        logging.debug(
            "Release monitor document generate for %s completed in %.1f ms (jira_cache=%s)",
            release_id,
            (datetime.now() - started).total_seconds() * 1000,
            bool(jira_snapshot.get("from_cache")),
        )
        increment_counter('release')
        return send_file(zip_buffer, as_attachment=True, download_name=f"{release_id}.zip")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logging.error("Ошибка формирования документов из блока релизов: %s", exc)
        return jsonify({"success": False, "error": "Не удалось сформировать документы"}), 500
