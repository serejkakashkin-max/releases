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
    
    sm_id, summary = extract_sm_id_and_summary(release_id)
    if sm_id and sm_id in ID_MAP:
        candidates = ID_MAP[sm_id]
        if len(candidates) == 1:
            category, release_name_clean = candidates[0]
            for clean, full in RELEASE_STRUCTURE.get(category, []):
                if clean == release_name_clean:
                    return jsonify({"found": True, "category": category, "release_clean": release_name_clean, "release_full": full, "candidates": None})
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
                        return jsonify({"found": True, "category": category, "release_clean": release_name_clean, "release_full": full, "candidates": None})
            candidates_list = []
            for cand_category, cand_release_clean in candidates:
                for clean, full in RELEASE_STRUCTURE.get(cand_category, []):
                    if clean == cand_release_clean:
                        candidates_list.append({"category": cand_category, "release_clean": cand_release_clean, "release_full": full})
                        break
            return jsonify({"found": False, "candidates": candidates_list})
    
    return jsonify({"found": False, "candidates": []})

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
