import os
import logging
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request

from services.dashboard_service import (
    get_dashboard_data, force_refresh_cache, 
    check_multiple_approvals, get_task_type_badges,
    get_hidden_tasks, get_hidden_task_keys, hide_task, show_task, restore_all_tasks
)
from services.release_monitor_service import (
    get_release_monitor_data,
    get_release_monitor_snapshot,
    start_release_monitor_refresh,
    get_release_monitor_refresh_status,
    get_release_monitor_reviewer_options,
    upload_release_monitor_duty_schedules,
    sync_release_monitor_assignments_from_confluence,
    save_release_monitor_manual_order,
    set_release_monitor_assignment,
    set_release_monitor_date_override,
    set_release_monitor_reviewer,
)
from services.report_service import save_report_to_disk
from services.release_report_service import get_release_report_service
from config import DASHBOARD_CACHE_TTL, DASHBOARD_ASSIGNEES_DISPLAY

BASE_PATH = os.getenv("BASE_PATH", "")

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/dashboard')
def dashboard():
    """Главная страница дашборда дежурного"""
    try:
        data = get_dashboard_data()
        last_update = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        
        # Получаем скрытые задачи
        hidden_tasks = get_hidden_tasks()
        hidden_task_keys = list(hidden_tasks.keys())
        
        # Фильтруем задачи - убираем скрытые
        def filter_hidden(tasks):
            return [t for t in tasks if t['key'] not in hidden_task_keys]
        
        sup_tasks = filter_hidden(data.get('sup_tasks', []))
        logi_tasks = filter_hidden(data.get('logi_tasks', []))
        vnedrenie_prom_tasks = filter_hidden(data.get('vnedrenie_prom_tasks', []))
        vnedrenie_psi_tasks = filter_hidden(data.get('vnedrenie_psi_tasks', []))
        release_monitor = data.get('release_monitor', [])
        release_monitor_summary = data.get('release_monitor_summary', {})
        release_monitor_meta = data.get('release_monitor_meta', {})
        
        # Подсчет общего количества для отображения
        total_sup = len(sup_tasks)
        total_logi = len(logi_tasks)
        total_vnedrenie = len(vnedrenie_prom_tasks) + len(vnedrenie_psi_tasks)
        
        # Подсчет активных дежурных (у кого есть задачи)
        assignee_stats = data.get('assignee_stats', {})
        
        # Фильтруем скрытые задачи из статистики дежурных
        for assignee in assignee_stats:
            assignee_stats[assignee]['todo'] = filter_hidden(assignee_stats[assignee].get('todo', []))
            assignee_stats[assignee]['in_progress'] = filter_hidden(assignee_stats[assignee].get('in_progress', []))
        
        dashboard_assignees = data.get('dashboard_assignees', DASHBOARD_ASSIGNEES_DISPLAY)
        active_assignees = sum(
            1 for assignee in dashboard_assignees
            if assignee_stats.get(assignee, {}).get('todo') or assignee_stats.get(assignee, {}).get('in_progress')
        )
        
        return render_template(
            'dashboard.html',
            basepath=BASE_PATH,
            sup_tasks=sup_tasks,
            logi_tasks=logi_tasks,
            vnedrenie_prom_tasks=vnedrenie_prom_tasks,
            vnedrenie_psi_tasks=vnedrenie_psi_tasks,
            assignee_stats=assignee_stats,
            dashboard_assignees=dashboard_assignees,
            last_update=last_update,
            cache_ttl_minutes=DASHBOARD_CACHE_TTL // 60,
            total_sup=total_sup,
            total_logi=total_logi,
            total_vnedrenie=total_vnedrenie,
            active_assignees=active_assignees,
            release_monitor=release_monitor,
            release_monitor_summary=release_monitor_summary,
            release_monitor_meta=release_monitor_meta,
            hidden_tasks=hidden_tasks,
            hidden_count=len(hidden_tasks)
        )
    except Exception as e:
        logging.error(f"Ошибка загрузки дашборда: {e}")
        return render_template(
            'dashboard.html',
            basepath=BASE_PATH,
            error="Ошибка загрузки данных из Jira. Попробуйте обновить страницу позже.",
            sup_tasks=[],
            logi_tasks=[],
            vnedrenie_prom_tasks=[],
            vnedrenie_psi_tasks=[],
            assignee_stats={},
            dashboard_assignees=DASHBOARD_ASSIGNEES_DISPLAY,
            last_update=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            cache_ttl_minutes=DASHBOARD_CACHE_TTL // 60,
            total_sup=0,
            total_logi=0,
            total_vnedrenie=0,
            active_assignees=0,
            release_monitor=[],
            release_monitor_summary={},
            release_monitor_meta={},
            hidden_tasks={},
            hidden_count=0
        )

@dashboard_bp.route('/release-monitor')
def release_monitor_page():
    """Отдельная страница контроля релизов."""
    try:
        release_monitor_data = get_release_monitor_snapshot()
        return render_template(
            'release_monitor.html',
            basepath=BASE_PATH,
            release_monitor=release_monitor_data.get('items', []),
            release_monitor_summary=release_monitor_data.get('summary', {}),
            release_monitor_meta=release_monitor_data.get('meta', {}),
            reviewer_options=get_release_monitor_reviewer_options(),
            last_update=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        )
    except Exception as e:
        logging.error(f"Ошибка загрузки страницы контроля релизов: {e}")
        return render_template(
            'release_monitor.html',
            basepath=BASE_PATH,
            release_monitor=[],
            release_monitor_summary={},
            release_monitor_meta={},
            reviewer_options=get_release_monitor_reviewer_options(),
            last_update=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            error="Ошибка загрузки данных по релизам. Попробуйте обновить страницу позже.",
        )

@dashboard_bp.route('/dashboard/refresh', methods=['POST'])
def refresh_dashboard():
    """Принудительное обновление данных дашборда"""
    try:
        force_refresh_cache()
        return jsonify({"success": True, "message": "Данные обновлены"})
    except Exception as e:
        logging.error(f"Ошибка принудительного обновления: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@dashboard_bp.route('/dashboard/release-monitor/report/current-week', methods=['POST'])
def generate_release_monitor_current_week_report():
    """Формирует HTML-отчет по предстоящим релизам текущей недели."""
    try:
        snapshot = get_release_monitor_snapshot() or {}
        items = snapshot.get('items', []) if isinstance(snapshot, dict) else []
        report_service = get_release_report_service()
        report_data = report_service.generate_current_week_plan_report(items)
        html_content = report_service.generate_current_week_plan_html(report_data)
        report_id = save_report_to_disk(html_content)

        return jsonify({
            "success": True,
            "download_url": f"{BASE_PATH}/dashboard/api/chat/report/download/{report_id}",
            "report_summary": {
                "total": report_data.get("statistics", {}).get("total", 0),
                "period": report_data.get("period", {}).get("label", ""),
            },
        })
    except Exception as e:
        logging.exception("Ошибка формирования недельного отчета по релизам")
        return jsonify({"success": False, "error": str(e)}), 500

@dashboard_bp.route('/dashboard/api/data', methods=['GET'])
def api_dashboard_data():
    """API endpoint для получения данных дашборда в JSON (для AJAX обновления)"""
    try:
        data = get_dashboard_data()
        vnedrenie_prom = data.get('vnedrenie_prom_tasks', [])
        vnedrenie_psi = data.get('vnedrenie_psi_tasks', [])
        return jsonify({
            "success": True,
            "sup_tasks": data.get('sup_tasks', []),
            "logi_tasks": data.get('logi_tasks', []),
            "vnedrenie_prom_tasks": vnedrenie_prom,
            "vnedrenie_psi_tasks": vnedrenie_psi,
            "release_monitor": data.get('release_monitor', []),
            "release_monitor_summary": data.get('release_monitor_summary', {}),
            "release_monitor_meta": data.get('release_monitor_meta', {}),
            "assignee_stats": data.get('assignee_stats', {}),
            "dashboard_assignees": data.get('dashboard_assignees', DASHBOARD_ASSIGNEES_DISPLAY),
            "last_update": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "total_sup": len(data.get('sup_tasks', [])),
            "total_logi": len(data.get('logi_tasks', [])),
            "total_vnedrenie": len(vnedrenie_prom) + len(vnedrenie_psi)
        })
    except Exception as e:
        logging.error(f"Ошибка API дашборда: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@dashboard_bp.route('/dashboard/release-monitor/refresh', methods=['POST'])
def refresh_release_monitor():
    """Запускает фоновое обновление блока релизов."""
    try:
        request_data = request.get_json(silent=True) or {}
        mode = (request_data.get("mode") or "full").strip().lower()
        if mode not in {"full", "quick"}:
            mode = "full"
        refresh_info = start_release_monitor_refresh(mode=mode, trigger="manual")
        return jsonify({
            "success": True,
            "started": refresh_info.get("started", False),
            "refresh_status": refresh_info.get("status", {}),
            "message": (
                "Полное обновление релизов запущено"
                if mode == "full" and refresh_info.get("started")
                else "Быстрое обновление релизов запущено"
                if mode == "quick" and refresh_info.get("started")
                else "Обновление релизов уже выполняется"
            )
        })
    except Exception as e:
        logging.error(f"Ошибка обновления блока релизов: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@dashboard_bp.route('/dashboard/release-monitor/status', methods=['GET'])
def release_monitor_status():
    """Возвращает статус фонового обновления и последний снимок данных релизов."""
    try:
        status_payload = get_release_monitor_refresh_status()
        release_monitor_data = status_payload.get("data", {})
        return jsonify({
            "success": True,
            "refresh_status": status_payload.get("status", {}),
            "release_monitor": release_monitor_data.get("items", []),
            "release_monitor_summary": release_monitor_data.get("summary", {}),
            "release_monitor_meta": release_monitor_data.get("meta", {}),
        })
    except Exception as e:
        logging.error(f"Ошибка получения статуса обновления релизов: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@dashboard_bp.route('/dashboard/release-monitor/reviewer', methods=['POST'])
def update_release_monitor_reviewer():
    """Сохраняет назначение по релизу: дежурный и проверяющий."""
    try:
        data = request.get_json(silent=True) or {}
        release_key = data.get("release_key", "")
        reviewer = data.get("reviewer", "")
        checker = data.get("checker", "")
        responsibles = data.get("responsibles", [])
        saved_assignment = set_release_monitor_assignment(release_key, reviewer, checker, responsibles)
        return jsonify({
            "success": True,
            "release_key": release_key,
            "reviewer": saved_assignment.get("reviewer", ""),
            "checker": saved_assignment.get("checker", ""),
            "responsibles": saved_assignment.get("responsibles", []),
        })
    except Exception as e:
        logging.error(f"Ошибка сохранения назначения по релизу: {e}")
        return jsonify({"success": False, "error": str(e)}), 400


@dashboard_bp.route('/dashboard/release-monitor/date-override', methods=['POST'])
def update_release_monitor_date_override():
    """Сохраняет ручную корректировку дат внедрения по строке релиза."""
    try:
        data = request.get_json(silent=True) or {}
        release_key = data.get("release_key", "")
        start_value = data.get("start", "")
        end_value = data.get("end", "")
        reset = bool(data.get("reset"))
        payload = set_release_monitor_date_override(release_key, start_value, end_value, reset=reset)
        return jsonify({
            "success": True,
            "release_monitor": payload.get("items", []),
            "release_monitor_summary": payload.get("summary", {}),
            "release_monitor_meta": payload.get("meta", {}),
        })
    except Exception as e:
        logging.error(f"Ошибка сохранения корректировки даты релиза: {e}")
        return jsonify({"success": False, "error": str(e)}), 400


@dashboard_bp.route('/dashboard/release-monitor/order', methods=['POST'])
def save_release_monitor_order():
    """Сохраняет ручной порядок релизов внутри групп выбранного года."""
    try:
        data = request.get_json(silent=True) or {}
        year = int(data.get("year") or datetime.now().year)
        waiting_row_keys = data.get("waiting_row_keys", [])
        numbered_row_keys = data.get("numbered_row_keys", [])
        result = save_release_monitor_manual_order(year, waiting_row_keys, numbered_row_keys)
        payload = result.get("data", {})
        return jsonify({
            "success": True,
            "year": result.get("year"),
            "release_monitor": payload.get("items", []),
            "release_monitor_summary": payload.get("summary", {}),
            "release_monitor_meta": payload.get("meta", {}),
        })
    except Exception as e:
        logging.error(f"Ошибка сохранения ручного порядка релизов: {e}")
        return jsonify({"success": False, "error": str(e)}), 400


@dashboard_bp.route('/dashboard/release-monitor/confluence-sync', methods=['POST'])
def sync_release_monitor_confluence():
    """Синхронизирует ответственных и проверяющих из эталонной страницы Confluence."""
    try:
        data = request.get_json(silent=True) or {}
        year = int(data.get("year") or datetime.now().year)
        sync_result = sync_release_monitor_assignments_from_confluence(year)
        payload = sync_result.get("data", {})
        return jsonify({
            "success": True,
            "message": f"Синхронизировано строк: {sync_result.get('matched_rows', 0)}",
            "matched_rows": sync_result.get("matched_rows", 0),
            "source_rows": sync_result.get("source_rows", 0),
            "year": sync_result.get("year"),
            "release_monitor": payload.get("items", []),
            "release_monitor_summary": payload.get("summary", {}),
            "release_monitor_meta": payload.get("meta", {}),
        })
    except Exception as e:
        logging.error(f"Ошибка синхронизации релизов с Confluence: {e}")
        return jsonify({"success": False, "error": str(e)}), 400


@dashboard_bp.route('/dashboard/release-monitor/duty-schedules/upload', methods=['POST'])
def upload_release_monitor_duty_files():
    """Загружает Excel-графики дежурств и автопроставляет дежурного в пустые релизы."""
    try:
        uploaded_files = request.files.getlist('files')
        result = upload_release_monitor_duty_schedules(uploaded_files)
        payload = result.get("data", {})
        return jsonify({
            "success": True,
            "message": f"Загружено графиков: {len(result.get('uploaded_files', []))}",
            "uploaded_files": result.get("uploaded_files", []),
            "parsed_months": result.get("parsed_months", []),
            "warnings": result.get("warnings", []),
            "applied_count": result.get("applied_count", 0),
            "release_monitor": payload.get("items", []),
            "release_monitor_summary": payload.get("summary", {}),
            "release_monitor_meta": payload.get("meta", {}),
        })
    except Exception as e:
        logging.error(f"Ошибка загрузки графиков дежурств: {e}")
        return jsonify({"success": False, "error": str(e)}), 400


@dashboard_bp.route('/dashboard/check-approvals', methods=['POST'])
def check_approvals():
    """Проверяет согласование для списка задач"""
    try:
        data = request.get_json()
        issue_keys = data.get('issue_keys', [])
        force_refresh = data.get('force_refresh', True)  # По умолчанию всегда обновляем
        
        if not issue_keys:
            return jsonify({"success": True, "approvals": {}})
        
        # Проверяем согласование для всех задач (с принудительным обновлением)
        approvals = check_multiple_approvals(issue_keys, force_refresh=force_refresh)
        
        return jsonify({
            "success": True,
            "approvals": approvals
        })
    except Exception as e:
        logging.error(f"Ошибка проверки согласований: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@dashboard_bp.route('/dashboard/clear-approval-cache', methods=['POST'])
def clear_approval_cache_route():
    """Очищает кэш согласований"""
    try:
        from services.dashboard_service import clear_approval_cache
        clear_approval_cache()
        return jsonify({"success": True, "message": "Кэш согласований очищен"})
    except Exception as e:
        logging.error(f"Ошибка очистки кэша: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@dashboard_bp.route('/dashboard/api/search', methods=['GET'])
def search_tasks():
    """Поиск по задачам дашборда"""
    try:
        query = request.args.get('q', '').lower().strip()
        
        if not query:
            return jsonify({"success": True, "results": []})
        
        data = get_dashboard_data()
        results = []
        
        # Ищем во всех типах задач
        all_tasks = (
            data.get('sup_tasks', []) + 
            data.get('logi_tasks', []) + 
            data.get('vnedrenie_tasks', [])
        )
        
        for task in all_tasks:
            # Ищем по ключу
            if query in task.get('key', '').lower():
                results.append(task)
                continue
            
            # Ищем по summary
            if query in task.get('summary', '').lower():
                results.append(task)
                continue
            
            # Ищем по исполнителю
            if query in task.get('assignee_name', '').lower():
                results.append(task)
                continue
            
            # Ищем по описанию
            if query in (task.get('description') or '').lower():
                results.append(task)
                continue
        
        # Убираем дубликаты
        seen_keys = set()
        unique_results = []
        for task in results:
            if task['key'] not in seen_keys:
                seen_keys.add(task['key'])
                unique_results.append(task)
        
        return jsonify({
            "success": True,
            "results": unique_results,
            "count": len(unique_results)
        })
    except Exception as e:
        logging.error(f"Ошибка поиска: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# === API для управления скрытыми задачами (Корзина) ===

@dashboard_bp.route('/dashboard/api/hidden-tasks', methods=['GET'])
def get_hidden_tasks_api():
    """Получает список всех скрытых задач"""
    try:
        hidden = get_hidden_tasks()
        return jsonify({
            "success": True,
            "hidden_tasks": hidden,
            "count": len(hidden)
        })
    except Exception as e:
        logging.error(f"Ошибка получения скрытых задач: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@dashboard_bp.route('/dashboard/api/hidden-tasks', methods=['POST'])
def hide_task_api():
    """Скрывает задачу (добавляет в корзину)"""
    try:
        data = request.get_json()
        task_key = data.get('task_key')
        task_data = data.get('task_data', {})
        
        if not task_key:
            return jsonify({"success": False, "error": "task_key is required"}), 400
        
        if hide_task(task_key, task_data):
            return jsonify({
                "success": True,
                "message": f"Задача {task_key} скрыта"
            })
        else:
            return jsonify({"success": False, "error": "Failed to hide task"}), 500
    except Exception as e:
        logging.error(f"Ошибка скрытия задачи: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# Маршрут для восстановления одной задачи через query parameter
@dashboard_bp.route('/dashboard/api/hidden-tasks/restore-one', methods=['POST'])
def show_task_api():
    """Показывает задачу (восстанавливает из корзины)"""
    try:
        data = request.get_json()
        task_key = data.get('task_key')
        
        if not task_key:
            return jsonify({"success": False, "error": "task_key is required"}), 400
        
        if show_task(task_key):
            return jsonify({
                "success": True,
                "message": f"Задача {task_key} восстановлена"
            })
        else:
            return jsonify({
                "success": False,
                "error": f"Задача {task_key} не найдена в корзине"
            }), 404
    except Exception as e:
        logging.error(f"Ошибка восстановления задачи: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@dashboard_bp.route('/dashboard/release-monitor/report/current-week-legacy', methods=['POST'])
def generate_release_monitor_current_week_report_legacy():
    """Формирует HTML-отчет по предстоящим релизам текущей недели."""
    try:
        snapshot = get_release_monitor_snapshot() or {}
        items = snapshot.get('items', []) if isinstance(snapshot, dict) else []
        report_service = get_release_report_service()
        report_data = report_service.generate_current_week_plan_report(items)
        html_content = report_service.generate_current_week_plan_html(report_data)
        report_id = save_report_to_disk(html_content)

        return jsonify({
            "success": True,
            "download_url": f"{BASE_PATH}/dashboard/api/chat/report/download/{report_id}",
            "report_summary": {
                "total": report_data.get("statistics", {}).get("total", 0),
                "period": report_data.get("period", {}).get("label", ""),
            },
        })
    except Exception as e:
        logging.exception("Ошибка формирования недельного отчета по релизам")
        return jsonify({"success": False, "error": str(e)}), 500


@dashboard_bp.route('/dashboard/api/hidden-tasks/restore-all', methods=['POST'])
def restore_all_tasks_api():
    """Восстанавливает все скрытые задачи"""
    try:
        count = restore_all_tasks()
        return jsonify({
            "success": True,
            "message": f"Восстановлено {count} задач",
            "restored_count": count
        })
    except Exception as e:
        logging.error(f"Ошибка восстановления всех задач: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
