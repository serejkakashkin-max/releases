"""
API routes для чат-бота дашборда дежурного.
"""

import logging
import os
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, session, send_file

from services.chatbot_service import (
    get_chatbot,
    ChatMessage,
    get_release_document_path,
    cleanup_old_release_documents,
)
from services.rov_statistics_service import (
    cleanup_old_rov_statistics_reports,
    get_rov_statistics_report_path,
)
from services.release_monitor_backup_service import (
    cleanup_old_release_monitor_cache_backups,
    get_release_monitor_cache_backup_path,
)
from services.dashboard_service import get_dashboard_data

chatbot_bp = Blueprint('chatbot', __name__)
BASE_PATH = os.getenv("BASE_PATH", "")

# In-memory storage for sessions (в продакшене лучше использовать Redis)
_chat_sessions = {}


def get_or_create_session_id():
    """Получает или создаёт ID сессии для чата"""
    if 'chat_session_id' not in session:
        session['chat_session_id'] = str(uuid.uuid4())
    return session['chat_session_id']


@chatbot_bp.route('/dashboard/api/chat', methods=['POST'])
def chat():
    """
    Основной endpoint для диалога с чат-ботом.
    
    Request body:
    {
        "message": "текст сообщения пользователя",
        "context": {}  # опционально - контекст дашборда
    }
    
    Response:
    {
        "success": true,
        "response": {
            "text": "ответ бота",
            "intent": "тип намерения",
            "suggestions": ["подсказка1", "подсказка2"],
            "metadata": {}
        }
    }
    """
    try:
        data = request.get_json()
        
        if not data or 'message' not in data:
            return jsonify({
                "success": False,
                "error": "Поле 'message' обязательно"
            }), 400
        
        message = data['message'].strip()
        if not message:
            return jsonify({
                "success": False,
                "error": "Сообщение не может быть пустым"
            }), 400
        
        # Получаем или создаём сессию
        session_id = get_or_create_session_id()
        
        # Получаем контекст дашборда (если передан)
        dashboard_context = data.get('context')
        
        needs_fresh_dashboard = not dashboard_context
        if dashboard_context and dashboard_context.get("page_context") == "home":
            task_lists = (
                dashboard_context.get("sup_tasks"),
                dashboard_context.get("logi_tasks"),
                dashboard_context.get("vnedrenie_prom_tasks"),
                dashboard_context.get("vnedrenie_psi_tasks"),
            )
            needs_fresh_dashboard = not any(task_lists)

        # Если контекст не передан или главная страница не несет рабочие данные,
        # подтягиваем их на backend, чтобы главный Oplot был независим от страницы.
        if needs_fresh_dashboard:
            try:
                fresh_context = get_dashboard_data()
                if dashboard_context:
                    fresh_context.update({k: v for k, v in dashboard_context.items() if v not in (None, "", [])})
                dashboard_context = fresh_context
            except Exception as e:
                logging.warning(f"Не удалось получить данные дашборда: {e}")
                dashboard_context = dashboard_context or {}
        
        # Обрабатываем сообщение
        chatbot = get_chatbot()
        result = chatbot.process_message(message, session_id, dashboard_context)
        
        return jsonify({
            "success": True,
            "response": result,
            "session_id": session_id
        })
        
    except Exception as e:
        logging.error(f"Ошибка в chat endpoint: {e}")
        return jsonify({
            "success": False,
            "error": f"Внутренняя ошибка: {str(e)}"
        }), 500


@chatbot_bp.route('/dashboard/api/chat/suggestions', methods=['GET'])
def get_suggestions():
    """
    Возвращает контекстные подсказки для чата.
    
    Query params:
    - intent: тип намерения (опционально)
    - context: текущий контекст (опционально)
    
    Response:
    {
        "success": true,
        "suggestions": [
            {"text": "Текст подсказки", "action": "действие"}
        ]
    }
    """
    try:
        from services.intent_classifier import IntentType
        
        intent_str = request.args.get('intent', 'unknown')
        
        # Базовые подсказки
        default_suggestions = [
            {"text": "Показать релизы недели по ответственному", "action": "release_week_query"},
            {"text": "Сформировать документы по релизу", "action": "release_documents"},
            {"text": "Инструкция ПСИ по релизу", "action": "release_psi_instruction"},
            {"text": "Выгрузить таблицу релизов в Confluence", "action": "release_confluence_export"},
            {"text": "Контроль недели", "action": "release_week_control"},
            {"text": "Сводка дневной смены", "action": "handover_day"},
            {"text": "Сформируй статистику", "action": "statistics"},
        ]

        contextual_suggestions = {
            IntentType.SEARCH_TASKS.value: [
                {"text": "Поиск задач", "action": "search_tasks"},
                {"text": "Сводка дневной смены", "action": "handover_day"},
                {"text": "Что ты умеешь", "action": "capabilities"},
            ],
            IntentType.GENERATE_REPORT.value: [
                {"text": "Сводка дневной смены", "action": "handover_day"},
                {"text": "Сводка вечерней смены", "action": "handover_evening"},
                {"text": "Контроль недели", "action": "release_week_control"},
                {"text": "Что ты умеешь", "action": "capabilities"},
            ],
            IntentType.SHOW_CAPABILITIES.value: [
                {"text": "Показать релизы недели по ответственному", "action": "release_week_query"},
                {"text": "Сформировать документы по релизу", "action": "release_documents"},
                {"text": "Инструкция ПСИ по релизу", "action": "release_psi_instruction"},
                {"text": "Выгрузить таблицу релизов в Confluence", "action": "release_confluence_export"},
                {"text": "Контроль недели", "action": "release_week_control"},
            ],
        }
        
        suggestions = contextual_suggestions.get(intent_str, default_suggestions)
        
        return jsonify({
            "success": True,
            "suggestions": suggestions,
            "intent": intent_str
        })
        
    except Exception as e:
        logging.error(f"Ошибка получения подсказок: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@chatbot_bp.route('/dashboard/api/chat/history', methods=['GET'])
def get_history():
    """
    Возвращает историю сообщений текущей сессии.
    
    Query params:
    - limit: максимальное количество сообщений (по умолчанию 20)
    
    Response:
    {
        "success": true,
        "history": [
            {"role": "user", "content": "...", "timestamp": "..."},
            {"role": "assistant", "content": "...", "timestamp": "..."}
        ]
    }
    """
    try:
        session_id = get_or_create_session_id()
        limit = request.args.get('limit', 20, type=int)
        
        chatbot = get_chatbot()
        chat_session = chatbot.get_or_create_session(session_id)
        
        history = chat_session.get_history(limit)
        
        # Форматируем для JSON
        history_data = [
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                "intent": msg.intent,
            }
            for msg in history
        ]
        
        return jsonify({
            "success": True,
            "history": history_data,
            "session_id": session_id
        })
        
    except Exception as e:
        logging.error(f"Ошибка получения истории: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@chatbot_bp.route('/dashboard/api/chat/clear', methods=['POST'])
def clear_history():
    """
    Очищает историю текущей сессии.
    
    Response:
    {
        "success": true,
        "message": "История очищена"
    }
    """
    try:
        session_id = get_or_create_session_id()
        
        chatbot = get_chatbot()
        if session_id in chatbot.sessions:
            chatbot.sessions[session_id].messages = []
            chatbot.sessions[session_id].pending_clarification = None
            chatbot.sessions[session_id].active_release_flow = None
        
        return jsonify({
            "success": True,
            "message": "История чата очищена"
        })
        
    except Exception as e:
        logging.error(f"Ошибка очистки истории: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@chatbot_bp.route('/dashboard/api/chat/quick-action', methods=['POST'])
def quick_action():
    """
    Выполняет быстрое действие из подсказок.
    
    Request body:
    {
        "action": "тип действия",
        "params": {}  # дополнительные параметры
    }
    
    Response:
    {
        "success": true,
        "response": {...}
    }
    """
    try:
        data = request.get_json()
        action = data.get('action')
        params = data.get('params', {})
        
        session_id = get_or_create_session_id()
        chatbot = get_chatbot()
        
        # Маппинг быстрых действий на сообщения
        action_messages = {
            'capabilities': 'Показать что я умею?',
            'search': 'Покажи все задачи',
            'search_closed': 'Покажи закрытые задачи',
            'search_logi_tag': 'Найди задачи с тегом логи',
            'search_summary': 'Найди задачу со словом "логи" в заголовке',
            'assignee_stats': 'Сгенерируй статистику',
            'statistics': 'Сформируй статистику',
            'handover_day': 'Сводка для передачи дневной смены',
            'handover_evening': 'Сводка для передачи вечерней смены',
            'release_documents': 'Сформировать документы по релизу',
            'release_psi_instruction': 'Дай инструкцию ПСИ по релизу',
            'rov_statistics': 'Статистика по РОВ',
        }

        message = action_messages.get(action, 'Показать что я умею?')
        
        # Получаем контекст дашборда
        try:
            dashboard_context = get_dashboard_data()
        except Exception as e:
            logging.warning(f"Не удалось получить данные дашборда: {e}")
            dashboard_context = {}
        
        # Обрабатываем как обычное сообщение
        result = chatbot.process_message(message, session_id, dashboard_context)
        
        return jsonify({
            "success": True,
            "response": result,
            "action": action
        })
        
    except Exception as e:
        logging.error(f"Ошибка выполнения быстрого действия: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@chatbot_bp.route('/dashboard/api/chat/report', methods=['POST'])
def generate_report():
    """
    Генерирует HTML отчёт по сотрудникам.
    
    Request body:
    {
        "days": 30  # опционально, по умолчанию 30
    }
    
    Response:
    {
        "success": true,
        "download_url": "/dashboard/api/chat/report/download/<id>",
        "report_summary": {
            "total_tasks": 100,
            "period": "2025-01-01 / 2025-01-30"
        }
    }
    """
    try:
        from services.report_service import get_report_service, save_report_to_disk
        
        data = request.get_json() or {}
        days = data.get('days', 30)
        
        # Генерируем отчёт
        report_service = get_report_service()
        report_data = report_service.generate_assignee_report(days=days)
        
        if report_data['total_tasks'] == 0:
            return jsonify({
                "success": False,
                "error": "Нет закрытых задач за указанный период"
            }), 404
        
        # Генерируем HTML
        html_content = report_service.generate_html_report(report_data)
        
        # Сохраняем в папку reports/ с уникальным ID
        report_id = save_report_to_disk(html_content)
        
        # Сохраняем в сессии
        session['last_report_id'] = report_id
        
        return jsonify({
            "success": True,
            "download_url": f"{BASE_PATH}/dashboard/api/chat/report/download/{report_id}",
            "report_summary": {
                "total_tasks": report_data['total_tasks'],
                "period": f"{report_data['period']['start']} / {report_data['period']['end']}",
                "assignee_count": report_data['statistics']['assignee_count']
            }
        })
        
    except Exception as e:
        logging.error(f"Ошибка генерации отчёта: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@chatbot_bp.route('/dashboard/api/chat/report/download/<report_id>', methods=['GET'])
def download_report(report_id):
    """
    Скачивает сгенерированный отчёт.
    """
    try:
        from flask import send_file
        import os
        from services.report_service import get_report_path, REPORTS_DIR
        
        # Проверяем валидность report_id (только буквы, цифры, подчеркивания и дефисы)
        import re
        if not re.match(r'^[\w\-]+$', report_id):
            return jsonify({
                "success": False,
                "error": "Некорректный ID отчёта"
            }), 400
        
        # Получаем путь к файлу
        report_path = get_report_path(report_id)
        
        if not report_path or not os.path.exists(report_path):
            return jsonify({
                "success": False,
                "error": "Отчёт не найден или устарел"
            }), 404
        
        # Отправляем файл
        return send_file(
            report_path,
            mimetype='text/html',
            as_attachment=True,
            download_name=f"oplot_assignee_report_{report_id}.html"
        )
        
    except Exception as e:
        logging.error(f"Ошибка скачивания отчёта: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@chatbot_bp.route('/dashboard/api/chat/release-docs/download/<document_id>', methods=['GET'])
def download_release_documents(document_id):
    """Скачивает ZIP-документы релиза, сформированные через чат."""
    try:
        cleanup_old_release_documents()
        path = get_release_document_path(document_id)
        if not path or not os.path.exists(path):
            return jsonify({
                "success": False,
                "error": "Файл документов не найден или уже удален"
            }), 404

        return send_file(
            path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"release_documents_{document_id}.zip",
        )
    except Exception as e:
        logging.error(f"Ошибка скачивания релизных документов из чата: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@chatbot_bp.route('/dashboard/api/chat/rov-statistics/download/<report_id>', methods=['GET'])
def download_rov_statistics(report_id):
    """Скачивает Excel-выгрузку статистики по РОВ, сформированную через чат."""
    try:
        import re
        if not re.match(r'^[\w\-]+$', report_id):
            return jsonify({
                "success": False,
                "error": "Некорректный ID отчёта"
            }), 400

        cleanup_old_rov_statistics_reports()
        path = get_rov_statistics_report_path(report_id)
        if not path or not os.path.exists(path):
            return jsonify({
                "success": False,
                "error": "Excel-файл статистики не найден или устарел"
            }), 404

        return send_file(
            path,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=os.path.basename(path),
        )
    except Exception as e:
        logging.error(f"Ошибка скачивания статистики по РОВ: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@chatbot_bp.route('/dashboard/api/chat/release-monitor-cache/download/<backup_id>', methods=['GET'])
def download_release_monitor_cache_backup(backup_id):
    """Скачивает временный ZIP backup cache/state-файлов Блока релизов."""
    try:
        import re
        if not re.match(r'^[\w\-]+$', backup_id):
            return jsonify({
                "success": False,
                "error": "Некорректный ID backup"
            }), 400

        cleanup_old_release_monitor_cache_backups()
        path = get_release_monitor_cache_backup_path(backup_id)
        if not path or not os.path.exists(path):
            return jsonify({
                "success": False,
                "error": "Backup кэша не найден или уже удален"
            }), 404

        return send_file(
            path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=os.path.basename(path),
        )
    except Exception as e:
        logging.error(f"Ошибка скачивания backup кэша Блока релизов: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
