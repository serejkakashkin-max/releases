import os
import logging
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request

from services.dashboard_service import (
    get_dashboard_data, force_refresh_cache, 
    check_multiple_approvals, get_task_type_badges,
    get_hidden_tasks, get_hidden_task_keys, hide_task, show_task, restore_all_tasks
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
        
        active_assignees = sum(
            1 for stats in assignee_stats.values()
            if stats.get('todo') or stats.get('in_progress')
        )
        
        return render_template(
            'dashboard.html',
            basepath=BASE_PATH,
            sup_tasks=sup_tasks,
            logi_tasks=logi_tasks,
            vnedrenie_prom_tasks=vnedrenie_prom_tasks,
            vnedrenie_psi_tasks=vnedrenie_psi_tasks,
            assignee_stats=assignee_stats,
            dashboard_assignees=data.get('dashboard_assignees', DASHBOARD_ASSIGNEES),
            last_update=last_update,
            cache_ttl_minutes=DASHBOARD_CACHE_TTL // 60,
            total_sup=total_sup,
            total_logi=total_logi,
            total_vnedrenie=total_vnedrenie,
            active_assignees=active_assignees,
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
            dashboard_assignees=DASHBOARD_ASSIGNEES,
            last_update=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            cache_ttl_minutes=DASHBOARD_CACHE_TTL // 60,
            total_sup=0,
            total_logi=0,
            total_vnedrenie=0,
            active_assignees=0,
            hidden_tasks={},
            hidden_count=0
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
        vnedrenie_prom = data.get('vnedrenie_prom_tasks', [])
        vnedrenie_psi = data.get('vnedrenie_psi_tasks', [])
        return jsonify({
            "success": True,
            "sup_tasks": data.get('sup_tasks', []),
            "logi_tasks": data.get('logi_tasks', []),
            "vnedrenie_prom_tasks": vnedrenie_prom,
            "vnedrenie_psi_tasks": vnedrenie_psi,
            "assignee_stats": data.get('assignee_stats', {}),
            "dashboard_assignees": data.get('dashboard_assignees', DASHBOARD_ASSIGNEES),
            "last_update": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "total_sup": len(data.get('sup_tasks', [])),
            "total_logi": len(data.get('logi_tasks', [])),
            "total_vnedrenie": len(vnedrenie_prom) + len(vnedrenie_psi)
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