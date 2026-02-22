import os
import logging
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request

from services.dashboard_service import (
    get_dashboard_data, force_refresh_cache, 
    check_multiple_approvals, get_task_type_badges
)
from config import DASHBOARD_CACHE_TTL, DASHBOARD_ASSIGNEES

BASE_PATH = os.getenv("BASE_PATH", "")

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/dashboard')
def dashboard():
    """Главная страница дашборда дежурного"""
    try:
        data = get_dashboard_data()
        last_update = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        
        # Подсчет общего количества для отображения
        total_sup = len(data.get('sup_tasks', []))
        total_logi = len(data.get('logi_tasks', []))
        total_vnedrenie = len(data.get('vnedrenie_tasks', []))
        
        # Подсчет активных дежурных (у кого есть задачи)
        assignee_stats = data.get('assignee_stats', {})
        active_assignees = sum(
            1 for stats in assignee_stats.values() 
            if stats.get('todo') or stats.get('in_progress')
        )
        
        return render_template(
            'dashboard.html',
            basepath=BASE_PATH,
            sup_tasks=data.get('sup_tasks', []),
            logi_tasks=data.get('logi_tasks', []),
            vnedrenie_tasks=data.get('vnedrenie_tasks', []),
            assignee_stats=assignee_stats,
            dashboard_assignees=data.get('dashboard_assignees', DASHBOARD_ASSIGNEES),
            last_update=last_update,
            cache_ttl_minutes=DASHBOARD_CACHE_TTL // 60,
            total_sup=total_sup,
            total_logi=total_logi,
            total_vnedrenie=total_vnedrenie,
            active_assignees=active_assignees
        )
    except Exception as e:
        logging.error(f"Ошибка загрузки дашборда: {e}")
        return render_template(
            'dashboard.html',
            basepath=BASE_PATH,
            error="Ошибка загрузки данных из Jira. Попробуйте обновить страницу позже.",
            sup_tasks=[],
            logi_tasks=[],
            vnedrenie_tasks=[],
            assignee_stats={},
            dashboard_assignees=DASHBOARD_ASSIGNEES,
            last_update=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            cache_ttl_minutes=DASHBOARD_CACHE_TTL // 60,
            total_sup=0,
            total_logi=0,
            total_vnedrenie=0,
            active_assignees=0
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

@dashboard_bp.route('/dashboard/api/data', methods=['GET'])
def api_dashboard_data():
    """API endpoint для получения данных дашборда в JSON (для AJAX обновления)"""
    try:
        data = get_dashboard_data()
        return jsonify({
            "success": True,
            "sup_tasks": data.get('sup_tasks', []),
            "logi_tasks": data.get('logi_tasks', []),
            "vnedrenie_tasks": data.get('vnedrenie_tasks', []),
            "assignee_stats": data.get('assignee_stats', {}),
            "dashboard_assignees": data.get('dashboard_assignees', DASHBOARD_ASSIGNEES),
            "last_update": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "total_sup": len(data.get('sup_tasks', [])),
            "total_logi": len(data.get('logi_tasks', [])),
            "total_vnedrenie": len(data.get('vnedrenie_tasks', []))
        })
    except Exception as e:
        logging.error(f"Ошибка API дашборда: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

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