import os
import re
import logging
from datetime import datetime, timedelta
from services.gigachat_service import GIGA_HELPER
from pathlib import Path
from flask import Blueprint, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename
from zipfile import ZipFile
from io import BytesIO
import tempfile
from docx import Document  # ДОБАВЛЕНО

from extensions import RELEASE_STRUCTURE, ID_MAP  # Импортируем из extensions
from config import DOC_TEMPLATES_ROOT, DEFAULT_BH_PLAYBOOKS, OPLOT_VALUES
from services.jira_service import (
    get_release_version, get_issues_from_jira, get_ke_from_release, 
    get_pob_from_release, extract_sm_id_and_summary, get_distributives_info
)
from services.release_monitor_service import get_release_monitor_snapshot
from services.docx_service import replace_keys_in_doc, check_document
from services.gigachat_service import GIGA_HELPER
from services.counter_service import increment_counter  # НОВОЕ: импорт счетчика

BASE_PATH = os.getenv("BASE_PATH", "")

release_bp = Blueprint('release', __name__)

# УБРАНО: определение get_release_structure() - оно теперь в extensions.py


def release_uses_playbooks(release_name: str) -> bool:
    """Для части релизов плейбуки не используются."""
    release_name_upper = (release_name or "").upper()
    blocked_markers = ("SOWA", "ЕФС.AUTHENTICATION_USER", "AUTH", "RESSTORE(2889318)")
    return not any(marker in release_name_upper for marker in blocked_markers)


def detect_release_template(release_id: str):
    release_id = (release_id or "").strip()
    if not release_id:
        return {"found": False, "candidates": [], "error": "No release_id provided"}

    sm_id, summary = extract_sm_id_and_summary(release_id)
    if sm_id and sm_id in ID_MAP:
        candidates = ID_MAP[sm_id]
        if len(candidates) == 1:
            category, release_name_clean = candidates[0]
            for clean, full in RELEASE_STRUCTURE.get(category, []):
                if clean == release_name_clean:
                    return {
                        "found": True,
                        "category": category,
                        "release_clean": release_name_clean,
                        "release_full": full,
                        "candidates": None,
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
                            "found": True,
                            "category": category,
                            "release_clean": release_name_clean,
                            "release_full": full,
                            "candidates": None,
                        }
            candidates_list = []
            for cand_category, cand_release_clean in candidates:
                for clean, full in RELEASE_STRUCTURE.get(cand_category, []):
                    if clean == cand_release_clean:
                        candidates_list.append(
                            {
                                "category": cand_category,
                                "release_clean": cand_release_clean,
                                "release_full": full,
                            }
                        )
                        break
            return {"found": False, "candidates": candidates_list}

    return {"found": False, "candidates": []}


def _safe_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _get_previous_version_from_monitor_snapshot(row_key: str, release_id: str):
    snapshot = get_release_monitor_snapshot() or {}
    items = snapshot.get("items") or []
    if not items:
        return ""

    normalized_row_key = (row_key or "").strip()
    normalized_release_id = (release_id or "").strip()
    current_item = None

    if normalized_row_key:
        current_item = next(
            (item for item in items if str(item.get("row_key") or "").strip() == normalized_row_key),
            None,
        )

    if current_item is None and normalized_release_id:
        current_item = next(
            (item for item in items if str(item.get("release_key") or "").strip() == normalized_release_id),
            None,
        )

    if not current_item:
        return ""

    current_release_number = _safe_int(current_item.get("release_number"))
    current_ke_id = (current_item.get("ke_id") or "").strip()
    current_year = current_item.get("year")
    current_is_reroll = bool(current_item.get("is_reroll"))

    def _candidate_version(item):
        return str(item.get("release_version") or "").strip()

    def _candidate_sort_key(item):
        return (
            _safe_int(item.get("release_number")) or -1,
            str(item.get("release_key") or ""),
            str(item.get("row_key") or ""),
        )

    numbered_items = [
        item for item in items
        if _safe_int(item.get("release_number")) is not None
    ]

    if current_release_number is not None and current_ke_id:
        same_ke_current_year_candidates = [
            item for item in numbered_items
            if (item.get("ke_id") or "").strip() == current_ke_id
            and item.get("year") == current_year
            and _safe_int(item.get("release_number")) is not None
            and _safe_int(item.get("release_number")) < current_release_number
            and _candidate_version(item)
        ]
        if same_ke_current_year_candidates:
            return _candidate_version(max(same_ke_current_year_candidates, key=_candidate_sort_key))

        previous_year = _safe_int(current_year)
        previous_year = previous_year - 1 if previous_year is not None else None
        same_ke_previous_year_candidates = [
            item for item in numbered_items
            if (item.get("ke_id") or "").strip() == current_ke_id
            and previous_year is not None
            and _safe_int(item.get("year")) == previous_year
            and _candidate_version(item)
        ]
        if same_ke_previous_year_candidates:
            return _candidate_version(max(same_ke_previous_year_candidates, key=_candidate_sort_key))

    if current_is_reroll and current_release_number is not None:
        previous_numbered_candidates = [
            item for item in numbered_items
            if item.get("year") == current_year
            and _safe_int(item.get("release_number")) is not None
            and _safe_int(item.get("release_number")) < current_release_number
            and _candidate_version(item)
        ]
        if previous_numbered_candidates:
            return _candidate_version(max(previous_numbered_candidates, key=_candidate_sort_key))

    return ""


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
    prev_version: str,
    oplot: str,
    checker: str,
    instruction_link: str,
    date_str: str,
    ke: str,
    selected_playbooks,
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

    release_version = get_release_version(release_id)
    jira_issues = get_issues_from_jira(release_id)
    instruction_block = "Выполнить пункты инструкции по внедрению ИНСТРУКЦИЯ" if instruction_link else "Отсутствуют"
    pob = get_pob_from_release(release_id)
    playbooks_text = "\n".join(selected_playbooks)

    context = {
        "RELEASE_VERSION": release_version,
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

@release_bp.route('/get_ke')
def get_ke():
    release_id = request.args.get('release_id')
    ke = get_ke_from_release(release_id)
    return jsonify({'ke': ke})

@release_bp.route('/get_releases')
def get_releases():
    category = request.args.get('category')
    releases = RELEASE_STRUCTURE.get(category, [])
    return jsonify([{"clean": clean, "full": full} for clean, full in releases])

@release_bp.route('/auto_detect')
def auto_detect():
    release_id = request.args.get('release_id')
    if not release_id:
        return jsonify({"error": "No release_id provided"}), 400

    return jsonify(detect_release_template(release_id))


@release_bp.route('/release/monitor-init', methods=['POST'])
def release_monitor_init():
    data = request.get_json(silent=True) or {}
    release_id = (data.get("release_id") or "").strip()
    row_key = (data.get("row_key") or "").strip()
    if not release_id:
        return jsonify({"success": False, "error": "Не указан номер релиза"}), 400

    detection = detect_release_template(release_id)
    if detection.get("error"):
        return jsonify({"success": False, "error": detection["error"]}), 400

    release_full = detection.get("release_full", "")
    playbooks_required = release_uses_playbooks(release_full) if detection.get("found") else None

    return jsonify({
        "success": True,
        "release_id": release_id,
        "detection": detection,
        "ke": (data.get("ke") or get_ke_from_release(release_id) or "").strip(),
        "playbooks_required": playbooks_required,
        "playbooks": DEFAULT_BH_PLAYBOOKS,
        "oplot": (data.get("oplot") or "").strip(),
        "checker": (data.get("checker") or "").strip(),
        "date": (data.get("date") or "").strip(),
        "prev_version": _get_previous_version_from_monitor_snapshot(row_key, release_id),
    })


@release_bp.route('/release/monitor-generate', methods=['POST'])
def release_monitor_generate():
    data = request.get_json(silent=True) or {}
    release_id = (data.get("release_id") or "").strip()
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

    if not category or not release_full:
        detection = detect_release_template(release_id)
        if not detection.get("found"):
            return jsonify({
                "success": False,
                "error": "Не удалось автоопределить шаблон релиза. Используйте стандартный генератор или выберите шаблон вручную.",
                "detection": detection,
            }), 400
        category = detection.get("category", "")
        release_full = detection.get("release_full", "")

    if not release_uses_playbooks(release_full):
        selected_playbooks = []

    try:
        zip_buffer = _generate_release_zip_buffer(
            category=category,
            release_full=release_full,
            release_id=release_id,
            prev_version=prev_version,
            oplot=oplot,
            checker=checker,
            instruction_link=instruction_link,
            date_str=date_str,
            ke=ke,
            selected_playbooks=selected_playbooks,
        )
        increment_counter('release')
        return send_file(zip_buffer, as_attachment=True, download_name=f"{release_id}.zip")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logging.error("Ошибка формирования документов из блока релизов: %s", exc)
        return jsonify({"success": False, "error": "Не удалось сформировать документы"}), 500

@release_bp.route('/release', methods=['GET', 'POST'])
def release():
    categories = list(RELEASE_STRUCTURE.keys())
    oplot_values = OPLOT_VALUES
    playbooks = DEFAULT_BH_PLAYBOOKS
    status = ""
    results = {}
    current_date = datetime.now().strftime("%d.%m.%Y")
    
    if request.method == 'POST':
        required_fields = ['release_id', 'prev_version', 'oplot', 'checker', 'date']
        for field in required_fields:
            if not request.form.get(field):
                status = f"Ошибка: поле {field} обязательно для заполнения"
                return render_template('release.html', basepath=BASE_PATH, categories=categories, releases=[], oplot_values=oplot_values, playbooks=playbooks, status=status, current_date=current_date, results=results)
        
        category = request.form.get('category', '')
        release_full = request.form.get('release', '')
        release_id = request.form['release_id']
        prev_version = request.form['prev_version']
        oplot = request.form['oplot']
        checker = request.form['checker']
        instruction_link = request.form['instruction_link']
        date_str = request.form['date']
        ke = request.form['ke']
        selected_playbooks = request.form.getlist('playbooks')
        if not release_uses_playbooks(release_full):
            selected_playbooks = []
        playbooks_text = "\n".join(selected_playbooks)
        
        try:
            t_date = datetime.strptime(date_str, "%d.%m.%Y")
            t = t_date.strftime("%d.%m.%Y")
            tt = (t_date + timedelta(days=1)).strftime("%d.%m.%Y")
        except ValueError:
            status = "Ошибка: Неверный формат даты"
            return render_template('release.html', basepath=BASE_PATH, categories=categories, releases=[], oplot_values=oplot_values, playbooks=playbooks, status=status, current_date=current_date, results=results)
        
        release_version = get_release_version(release_id)
        jira_issues = get_issues_from_jira(release_id)
        instruction_block = "Выполнить пункты инструкции по внедрению ИНСТРУКЦИЯ" if instruction_link else "Отсутствуют"
        pob = get_pob_from_release(release_id)
        
        context = {
            "RELEASE_VERSION": release_version,
            "PREV_VERSION": prev_version,
            "RELEASE_ID": release_id,
            "OPLOT": oplot,
            "CHECKER": checker,
            "DATE": t,
            "PLUS_1": tt,
            "PLAYBOOKS": playbooks_text,
            "INSTRUCTION_BLOCK": instruction_block,
            "POB": pob,
            "RELNUMBER": release_id
        }
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            if not category or not release_full:
                status = "Ошибка: Не выбраны категория и/или релиз. Используйте автоопределение или выберите вручную."
                return render_template('release.html', basepath=BASE_PATH, categories=categories, releases=[], oplot_values=oplot_values, playbooks=playbooks, status=status, current_date=current_date, results=results)
            
            template_dir = DOC_TEMPLATES_ROOT / category / release_full
            
            if not template_dir.exists():
                status = f"Ошибка: директория с шаблонами не найдена: {template_dir}"
                return render_template('release.html', basepath=BASE_PATH, categories=categories, releases=[], oplot_values=oplot_values, playbooks=playbooks, status=status, current_date=current_date, results=results)
            
            template_files = list(template_dir.glob("*.docx"))
            
            if not template_files:
                status = f"Ошибка: шаблоны не найдены в директории: {template_dir}"
                return render_template('release.html', basepath=BASE_PATH, categories=categories, releases=[], oplot_values=oplot_values, playbooks=playbooks, status=status, current_date=current_date, results=results)
            
            has_errors = False
            results = {}
            generated_docs = []
            
            for path in template_files:
                doc = Document(path)
                doc = replace_keys_in_doc(doc, context, jira_issues, release_id, instruction_url=instruction_link if "План" in path.name else None)
                stem = path.stem
                if ke:
                    stem = stem.replace("КЭ", ke)
                
                output_path = temp_path / f"{stem}.docx"
                doc.save(output_path)
                generated_docs.append(output_path)
                
                if request.form.get('action') in ['check', 'recommendations']:
                    errors = check_document(output_path, context, jira_issues, release_id)
                    results[output_path.name] = errors
                    if errors:
                        has_errors = True
            
            if request.form.get('action') == 'check':
                status = "Результаты проверки документов"
                return render_template('release.html', basepath=BASE_PATH, categories=categories, releases=[], oplot_values=oplot_values, playbooks=playbooks, status=status, current_date=current_date, results=results)
            
            if request.form.get('action') == 'recommendations':
                dist_info = get_distributives_info(release_id)
                check_results = {}
                for doc_path in generated_docs:
                    errors = check_document(doc_path, context, jira_issues, release_id)
                    check_results[doc_path.name] = errors
                
                summary_text = GIGA_HELPER.generate_recommendations(check_results, dist_info)
                return render_template('recommendations.html', basepath=BASE_PATH, check_results=check_results, dist_info=dist_info, summary_text=summary_text, release_id=release_id)
            
            # НОВОЕ: Инкремент счетчика при успешной генерации
            increment_counter('release')
            
            zip_buffer = BytesIO()
            with ZipFile(zip_buffer, 'w') as zip_file:
                for doc_path in generated_docs:
                    zip_file.write(doc_path, doc_path.name)
            zip_buffer.seek(0)
            return send_file(zip_buffer, as_attachment=True, download_name=f"{release_id}.zip")
    
    return render_template('release.html', basepath=BASE_PATH, categories=categories, releases=[], oplot_values=oplot_values, playbooks=playbooks, status=status, current_date=current_date, results=results)
