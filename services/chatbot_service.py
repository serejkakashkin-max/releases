"""
Сервис чат-бота для дашборда дежурного.
Обрабатывает запросы пользователя и формирует ответы с помощью ГигаЧат.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

from services.intent_classifier import IntentClassifier, IntentType, get_intent_classifier
from services.gigachat_service import GIGA_HELPER
from services.dashboard_service import (
    get_dashboard_data, fetch_jira_tasks, process_tasks_data,
    DASHBOARD_ASSIGNEES, DASHBOARD_DAYS_BACK, get_hidden_task_keys
)
from services.jira_service import get_jira_domain_and_token
from services.chatbot_search_service import get_search_service
import requests
from services.report_service import save_report_to_disk
from services.release_monitor_service import (
    get_release_monitor_snapshot,
    get_release_monitor_week_control,
    get_release_monitor_week_responsible_recommendations,
    set_release_monitor_assignment,
    sync_release_monitor_assignments_from_confluence,
    sync_release_monitor_jira_fields,
    set_release_monitor_manual_distribution_override,
)
from services.release_report_service import get_release_report_service
from services.psi_jenkins_service import find_psi_jenkins_instructions_by_ke
from config import OPLOT_VALUES


BASE_PATH = os.getenv("BASE_PATH", "")
RELEASE_DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "release_docs")
RELEASE_DOCS_MAX_AGE_HOURS = int(os.getenv("RELEASE_DOCS_MAX_AGE_HOURS", "1"))


def get_release_document_path(document_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(document_id or ""))
    if not safe_id:
        return ""
    path = os.path.abspath(os.path.join(RELEASE_DOCS_DIR, f"{safe_id}.zip"))
    root = os.path.abspath(RELEASE_DOCS_DIR)
    if not path.startswith(root):
        return ""
    return path


def cleanup_old_release_documents(max_age_hours: int = RELEASE_DOCS_MAX_AGE_HOURS) -> int:
    if not os.path.exists(RELEASE_DOCS_DIR):
        return 0

    now = datetime.now()
    removed_count = 0
    root = os.path.abspath(RELEASE_DOCS_DIR)

    for filename in os.listdir(RELEASE_DOCS_DIR):
        if not filename.endswith(".zip"):
            continue

        filepath = os.path.abspath(os.path.join(RELEASE_DOCS_DIR, filename))
        if not filepath.startswith(root):
            continue

        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            age_hours = (now - mtime).total_seconds() / 3600
            if age_hours > max_age_hours:
                os.remove(filepath)
                removed_count += 1
                logging.info("Release document cache: removed old ZIP %s", filename)
        except Exception as exc:
            logging.warning("Release document cache: failed to remove %s: %s", filename, exc)

    return removed_count


@dataclass
class ChatMessage:
    """Сообщение в чате"""
    role: str  # 'user' или 'assistant'
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    intent: Optional[str] = None
    metadata: Dict = field(default_factory=dict)


@dataclass
class ChatContext:
    """Контекст сессии чата"""
    session_id: str
    messages: List[ChatMessage] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    pending_clarification: Optional[Dict[str, Any]] = None
    active_release_flow: Optional[Dict[str, Any]] = None
    
    def add_message(self, role: str, content: str, intent: Optional[str] = None, metadata: Dict = None):
        """Добавляет сообщение в историю"""
        self.messages.append(ChatMessage(
            role=role,
            content=content,
            intent=intent,
            metadata=metadata or {}
        ))
        # Ограничиваем историю последними 20 сообщениями
        if len(self.messages) > 20:
            self.messages = self.messages[-20:]
    
    def get_history(self, limit: int = 10) -> List[ChatMessage]:
        """Возвращает последние N сообщений"""
        return self.messages[-limit:]


class DashboardChatBot:
    """Чат-бот для рабочего стола дежурного"""
    
    def __init__(self):
        self.intent_classifier = get_intent_classifier()
        self.giga_helper = GIGA_HELPER
        self.search_service = get_search_service()
        self.sessions: Dict[str, ChatContext] = {}
        self.max_context_age_hours = 2
    
    def get_or_create_session(self, session_id: str) -> ChatContext:
        """Получает или создаёт сессию чата"""
        if session_id not in self.sessions:
            self.sessions[session_id] = ChatContext(session_id=session_id)
        return self.sessions[session_id]

    def get_default_suggestions(self) -> List[str]:
        return [
            "Релизы недели по ответственному",
            "Сформировать документы по релизу",
            "Инструкция ПСИ по релизу",
            "Выгрузить таблицу релизов в Confluence",
            "Контроль недели",
            "Сводка дневной смены",
            "Сформируй статистику",
        ]

    def _release_work_suggestions(self) -> List[str]:
        return [
            "Релизы недели по ответственному",
            "Сформировать документы по релизу",
            "Инструкция ПСИ по релизу",
            "Выгрузить таблицу релизов в Confluence",
            "Контроль недели",
            "Что ты умеешь",
        ]

    def _sanitize_suggestions_for_active_flow(self, session: ChatContext, suggestions: Optional[List[str]]) -> List[str]:
        """Не показываем быстрые кнопки, которые ломают текущий пошаговый сценарий."""
        flow = session.active_release_flow or {}
        flow_type = flow.get("type")
        state = flow.get("state")
        current = list(suggestions or [])

        if flow_type == "release_document_flow":
            if state == "need_release_key":
                return ["Отмена"]
            if state == "checker_requested":
                return ["Отмена"]
            if state == "distribution_requested":
                return ["Отмена"]
            if state == "instruction_requested":
                return ["Инструкции нет", "Отмена"]
            if state == "prev_version_requested":
                return ["Отмена"]
            if state == "playbooks_requested":
                return ["Плейбуки не нужны", "Отмена"]
            if state == "template_choice_requested":
                return current or ["Отмена"]
            if state in {"prev_version_confirm", "zni_confirm"}:
                return current or ["Отмена"]

        if flow_type == "release_psi_instruction" and state == "need_release_key":
            return ["Отмена"]

        if flow_type == "release_week_assignee" and state == "need_surname":
            return ["Отмена"]

        return current
    
    def process_message(self, message: str, session_id: str, dashboard_context: Dict = None) -> Dict:
        """
        Обрабатывает сообщение пользователя.
        
        Args:
            message: Сообщение пользователя
            session_id: ID сессии
            dashboard_context: Контекст дашборда (текущие задачи и т.д.)
            
        Returns:
            Dict: Ответ бота с метаданными
        """
        try:
            session = self.get_or_create_session(session_id)

            clarification_response = self._handle_clarification_reply(session, message, dashboard_context)
            if clarification_response:
                session.add_message('user', message, 'clarification_reply')
                session.add_message('assistant', clarification_response['text'], metadata=clarification_response.get('metadata', {}))
                return {
                    'text': clarification_response['text'],
                    'intent': clarification_response.get('intent', 'clarification'),
                    'suggestions': clarification_response.get('suggestions', []),
                    'metadata': clarification_response.get('metadata', {})
                }

            shift_response = self._handle_shift_handover_shortcut(message, dashboard_context)
            if shift_response:
                session.add_message('user', message, shift_response.get('intent', 'generate_report'))
                session.add_message('assistant', shift_response['text'], metadata=shift_response.get('metadata', {}))
                return {
                    'text': shift_response['text'],
                    'intent': shift_response.get('intent', 'generate_report'),
                    'suggestions': shift_response.get('suggestions', []),
                    'metadata': shift_response.get('metadata', {})
                }

            # Сначала используем локальную классификацию, затем при наличии GigaChat
            # даем модели шанс нормализовать "человеческую" формулировку.
            release_agent_response = self._handle_release_agent_command(message, session, dashboard_context)
            if release_agent_response:
                suggestions = self._sanitize_suggestions_for_active_flow(session, release_agent_response.get('suggestions', []))
                session.add_message('user', message, release_agent_response.get('intent', 'release_agent'))
                session.add_message('assistant', release_agent_response['text'], metadata=release_agent_response.get('metadata', {}))
                return {
                    'text': release_agent_response['text'],
                    'intent': release_agent_response.get('intent', 'release_agent'),
                    'suggestions': suggestions,
                    'metadata': release_agent_response.get('metadata', {})
                }

            local_intent = self.intent_classifier.classify(message)
            resolved_intent, normalized_message, ai_plan = self._resolve_intent_and_message(
                message,
                local_intent,
                dashboard_context
            )
            if self._is_release_report_request((normalized_message or message).lower(), dashboard_context):
                resolved_intent = IntentType.GENERATE_REPORT

            release_ai_response = self._execute_release_ai_action(
                ai_plan,
                message=message,
                normalized_message=normalized_message,
                session=session,
                dashboard_context=dashboard_context,
            )
            if release_ai_response:
                suggestions = self._sanitize_suggestions_for_active_flow(session, release_ai_response.get('suggestions', []))
                session.add_message('user', message, release_ai_response.get('intent', 'release_agent'))
                session.add_message('assistant', release_ai_response['text'], metadata=release_ai_response.get('metadata', {}))
                return {
                    'text': release_ai_response['text'],
                    'intent': release_ai_response.get('intent', 'release_agent'),
                    'suggestions': suggestions,
                    'metadata': {
                        **release_ai_response.get('metadata', {}),
                        **({'normalized_message': normalized_message} if normalized_message != message else {})
                    }
                }

            params = self._extract_params(normalized_message, resolved_intent)

            if resolved_intent == IntentType.SEARCH_TASKS:
                params['_original_message'] = normalized_message

            clarification_prompt = self._build_clarification_prompt(
                session=session,
                original_message=message,
                local_intent=local_intent,
                resolved_intent=resolved_intent,
                normalized_message=normalized_message,
                ai_plan=ai_plan
            )
            if clarification_prompt:
                response = clarification_prompt
            elif resolved_intent == IntentType.GREETING:
                response = self._handle_greeting()
            elif resolved_intent == IntentType.SEARCH_TASKS:
                response = self._handle_search(params, dashboard_context)
            elif resolved_intent == IntentType.GENERATE_REPORT:
                response = self._handle_report(params, dashboard_context, normalized_message)
            elif resolved_intent == IntentType.SPECIFIC_TASK:
                response = self._handle_specific_task(params, dashboard_context)
            elif resolved_intent == IntentType.SHOW_CAPABILITIES:
                response = self._handle_show_capabilities()
            elif ai_plan and ai_plan.get('action') == 'free_chat':
                response = self._ask_gigachat(message, session, dashboard_context)
            else:
                response = self._ask_gigachat(message, session, dashboard_context)
            
            # Сохраняем в историю
            session.add_message('user', message, resolved_intent.value if resolved_intent else None)
            session.add_message('assistant', response['text'], metadata=response.get('metadata', {}))
            
            # Формируем результат
            suggestions = self._sanitize_suggestions_for_active_flow(
                session,
                response.get('suggestions', self.intent_classifier.get_suggestions(resolved_intent))
            )
            return {
                'text': response['text'],
                'intent': resolved_intent.value if resolved_intent else 'unknown',
                'suggestions': suggestions,
                'metadata': {
                    **response.get('metadata', {}),
                    **({'normalized_message': normalized_message} if normalized_message != message else {})
                }
            }
            
        except Exception as e:
            logging.error(f"Ошибка обработки сообщения: {e}")
            return {
                'text': 'Произошла ошибка при обработке запроса. Попробуйте переформулировать.',
                'intent': 'error',
                'suggestions': ['Показать что я умею?', 'Сгенерировать статистику'],
                'metadata': {'error': str(e)}
            }

    def _resolve_intent_and_message(
        self,
        message: str,
        local_intent: IntentType,
        dashboard_context: Dict = None
    ) -> Tuple[IntentType, str, Optional[Dict]]:
        """Разрешает intent и нормализованное сообщение через GigaChat, если он доступен."""
        ai_plan = self._plan_with_gigachat(message, dashboard_context, local_intent)
        if not ai_plan:
            return local_intent, message, None

        ai_intent = self._intent_from_ai_action(ai_plan.get('action'))
        normalized_message = ai_plan.get('normalized_message') or message

        if ai_plan.get('action') == 'free_chat':
            return IntentType.UNKNOWN, message, ai_plan

        if ai_intent is None:
            return local_intent, message, ai_plan

        # Для устойчивых рабочих сценариев позволяем AI исправлять формулировку,
        # но не ломаем уже распознанный конкретный ключ задачи.
        if local_intent == IntentType.SPECIFIC_TASK and ai_intent != IntentType.SPECIFIC_TASK:
            return local_intent, message, ai_plan

        if local_intent == IntentType.UNKNOWN or ai_intent == local_intent:
            return ai_intent, normalized_message, ai_plan

        # Если AI уверенно свел запрос к поддерживаемому рабочему сценарию, используем его.
        if ai_intent in {
            IntentType.SEARCH_TASKS,
            IntentType.GENERATE_REPORT,
            IntentType.SPECIFIC_TASK,
            IntentType.SHOW_CAPABILITIES,
            IntentType.GREETING,
        }:
            return ai_intent, normalized_message, ai_plan

        return local_intent, message, ai_plan

    def _intent_from_ai_action(self, action: Optional[str]) -> Optional[IntentType]:
        """Преобразует AI action в локальный IntentType."""
        mapping = {
            'search_tasks': IntentType.SEARCH_TASKS,
            'generate_report': IntentType.GENERATE_REPORT,
            'specific_task': IntentType.SPECIFIC_TASK,
            'show_capabilities': IntentType.SHOW_CAPABILITIES,
            'greeting': IntentType.GREETING,
        }
        return mapping.get(action)

    def _execute_release_ai_action(
        self,
        ai_plan: Optional[Dict],
        *,
        message: str,
        normalized_message: str,
        session: ChatContext,
        dashboard_context: Dict = None,
    ) -> Optional[Dict]:
        """Выполняет релизные действия, которые GigaChat распознал в свободной формулировке."""
        if not ai_plan:
            return None

        action = str(ai_plan.get('action') or '').strip()
        if not action:
            return None

        command_text = (ai_plan.get('normalized_message') or normalized_message or message or '').strip()
        release_actions = {
            'release_documents',
            'release_confluence_export',
            'release_week_query',
            'release_week_control',
            'release_week_recommendations',
            'release_current_week_report',
            'release_statistics',
            'release_psi_instruction',
        }
        if action not in release_actions:
            return None

        if action == 'release_documents':
            return self._handle_release_document_query(command_text, session)
        if action == 'release_confluence_export':
            return self._handle_release_confluence_export_query()
        if action == 'release_week_query':
            return self._handle_release_week_assignee_query(command_text, session=session)
        if action == 'release_week_control':
            return self._handle_release_week_control()
        if action == 'release_week_recommendations':
            return self._handle_release_week_recommendations(session=session)
        if action == 'release_current_week_report':
            return self._handle_current_week_release_report()
        if action == 'release_statistics':
            return self._handle_release_statistics({}, dashboard_context, command_text)
        if action == 'release_psi_instruction':
            return self._handle_psi_jenkins_instruction_query(command_text, session=session)

        return None

    def _plan_with_gigachat(
        self,
        message: str,
        dashboard_context: Dict = None,
        local_intent: IntentType = IntentType.UNKNOWN
    ) -> Optional[Dict]:
        """Просит GigaChat определить сценарий и при необходимости нормализовать запрос."""
        if not self.giga_helper.client:
            return None

        dashboard_summary = self._get_dashboard_summary(dashboard_context)
        prompt = f"""Ты маршрутизатор запросов для единого AI-бота Oplot.

Oplot умеет работать с рабочим столом дежурного, Блоком релизов, документами и Confluence.
Не придумывай данные и новые действия. Если параметров мало, выбирай ближайшее действие с confidence=medium или low, чтобы локальная логика задала уточнение.

Текущий локальный intent: {local_intent.value}

Поддерживаемые действия:
- release_documents: оформить или сформировать документы по релизу
- release_confluence_export: выгрузить таблицу релизов в Confluence
- release_week_query: показать релизы текущей недели по ответственному
- release_week_control: контроль недели, релизы без ответственного, доступные/исключенные кандидаты
- release_week_recommendations: предложить или порекомендовать ответственных по релизам недели
- release_current_week_report: сформировать HTML-отчет или сводку по релизам текущей недели
- release_statistics: релизная статистика, аналитика по релизам
- release_psi_instruction: найти инструкцию/джобу Jenkins для раскатки на ПСИ по релизу
- search_tasks: поиск или показ задач Jira/OPLOT
- generate_report: статистика, отчеты, сводка смены, релизная аналитика
- specific_task: запрос по конкретному ключу Jira
- show_capabilities: показать возможности бота
- greeting: приветствие
- free_chat: свободный разговор вне рабочих сценариев
- unknown: запрос неясен

Правила:
- Если пользователь просит "сводку смены", "сводку дневной смены", "сводку вечерней смены" или "передачу смены", выбирай generate_report, а не release_statistics.
- Если пользователь просит "оформить", "собрать пакет", "подготовить документы" и указан ключ релиза, выбирай release_documents.
- Если пользователь спрашивает "релизы за/у/для <фамилия>", выбирай release_week_query, даже если слово "ответственный" не написано.
- Если пользователь просит "предложи ответственных" или "кого назначить", выбирай release_week_recommendations.
- Если пользователь просит инструкцию, джобу Jenkins, параметры или раскатку на ПСИ по конкретному релизу, выбирай release_psi_instruction.
- Если пользователь просто общается или задает сторонний вопрос без рабочих маркеров, выбирай free_chat.

Контекст:
{dashboard_summary}

Запрос пользователя:
{message}

Верни только JSON:
{{
  "action": "one_of_supported_actions",
  "normalized_message": "короткая нормализованная формулировка на русском или пустая строка",
  "confidence": "high|medium|low"
}}"""
        try:
            response = self.giga_helper.client.chat(prompt)
            content = response.choices[0].message.content
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group())
            return {
                'action': data.get('action'),
                'normalized_message': str(data.get('normalized_message') or '').strip(),
                'confidence': data.get('confidence', 'low')
            }
        except Exception as e:
            logging.warning(f"Не удалось получить AI-план запроса: {e}")
            return None

    def _build_clarification_prompt(
        self,
        session: ChatContext,
        original_message: str,
        local_intent: IntentType,
        resolved_intent: IntentType,
        normalized_message: str,
        ai_plan: Optional[Dict]
    ) -> Optional[Dict]:
        """Формирует уточняющий вопрос, если рабочий запрос распознан неуверенно."""
        suggestions: List[str] = []
        confidence = (ai_plan or {}).get('confidence', 'low')
        action = (ai_plan or {}).get('action')
        is_work_request = self._looks_like_work_request(original_message)

        if action and action != 'free_chat' and confidence in {'low', 'medium'}:
            suggestions.append(normalized_message)

        if is_work_request and local_intent == IntentType.UNKNOWN:
            suggestions.extend([
                "Сгенерировать статистику",
                "Сводка для передачи дневной смены",
                "Показать что я умею?",
            ])

        suggestions = [item for item in suggestions if item]
        deduped_suggestions = []
        seen = set()
        for item in suggestions:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped_suggestions.append(item)

        if not deduped_suggestions:
            return None

        session.pending_clarification = {
            'original_message': original_message,
            'resolved_intent': resolved_intent.value,
            'suggestions': deduped_suggestions,
            'created_at': datetime.now().isoformat()
        }

        text = "Не до конца понял рабочий запрос.\n\n"
        text += "Возможно, вы имели в виду:\n"
        release_suggestions = []
        for index, suggestion in enumerate(deduped_suggestions[:3], 1):
            text += f"{index}. {suggestion}\n"
        text += "\nОтветьте `да`, номером варианта, или напишите уточнение."

        suggestions = release_suggestions + ["Выгрузить таблицу релизов в Confluence", "Что ты умеешь?"]

        return {
            'text': text,
            'suggestions': deduped_suggestions[:3],
            'metadata': {'type': 'clarification', 'options': deduped_suggestions[:3]}
        }

    def _handle_clarification_reply(
        self,
        session: ChatContext,
        message: str,
        dashboard_context: Dict = None
    ) -> Optional[Dict]:
        """Обрабатывает ответ пользователя на уточняющий вопрос."""
        pending = session.pending_clarification
        if not pending:
            return None

        message_lower = message.strip().lower()
        suggestions = pending.get('suggestions', [])

        if message_lower in {'да', 'ага', 'угу', 'yes', 'ok', 'ок', 'верно'}:
            selected = suggestions[0] if suggestions else pending.get('original_message', '')
            session.pending_clarification = None
            return self._execute_clarified_message(selected, session, dashboard_context)

        if message_lower in {'нет', 'неа', 'no', 'неверно'}:
            session.pending_clarification = None
            return {
                'text': 'Уточните, что именно нужно сделать. Можно написать запрос свободно, я попробую понять снова.',
                'intent': 'clarification',
                'suggestions': ['Сгенерировать статистику', 'Сводка для передачи дневной смены', 'Показать что я умею?'],
                'metadata': {'type': 'clarification_retry'}
            }

        if message_lower.isdigit():
            index = int(message_lower) - 1
            if 0 <= index < len(suggestions):
                selected = suggestions[index]
                session.pending_clarification = None
                return self._execute_clarified_message(selected, session, dashboard_context)

        combined_message = f"{pending.get('original_message', '')}. Уточнение пользователя: {message}".strip()
        session.pending_clarification = None
        return self._execute_clarified_message(combined_message, session, dashboard_context)

    def _execute_clarified_message(
        self,
        message: str,
        session: ChatContext,
        dashboard_context: Dict = None
    ) -> Dict:
        """Повторно запускает обработку после уточнения."""
        shift_response = self._handle_shift_handover_shortcut(message, dashboard_context)
        if shift_response:
            return {
                'text': shift_response['text'],
                'intent': shift_response.get('intent', 'generate_report'),
                'suggestions': shift_response.get('suggestions', []),
                'metadata': shift_response.get('metadata', {})
            }

        local_intent = self.intent_classifier.classify(message)
        resolved_intent, normalized_message, ai_plan = self._resolve_intent_and_message(
            message,
            local_intent,
            dashboard_context
        )
        release_ai_response = self._execute_release_ai_action(
            ai_plan,
            message=message,
            normalized_message=normalized_message,
            session=session,
            dashboard_context=dashboard_context,
        )
        if release_ai_response:
            return {
                'text': release_ai_response['text'],
                'intent': release_ai_response.get('intent', 'release_agent'),
                'suggestions': release_ai_response.get('suggestions', []),
                'metadata': release_ai_response.get('metadata', {})
            }

        params = self._extract_params(normalized_message, resolved_intent)
        if resolved_intent == IntentType.SEARCH_TASKS:
            params['_original_message'] = normalized_message

        if resolved_intent == IntentType.SEARCH_TASKS:
            response = self._handle_search(params, dashboard_context)
        elif resolved_intent == IntentType.GENERATE_REPORT:
            response = self._handle_report(params, dashboard_context, normalized_message)
        elif resolved_intent == IntentType.SPECIFIC_TASK:
            response = self._handle_specific_task(params, dashboard_context)
        elif resolved_intent == IntentType.SHOW_CAPABILITIES:
            response = self._handle_show_capabilities()
        elif ai_plan and ai_plan.get('action') == 'free_chat':
            response = self._ask_gigachat(message, session, dashboard_context)
        else:
            response = self._ask_gigachat(message, session, dashboard_context)

        return {
            'text': response['text'],
            'intent': resolved_intent.value if resolved_intent else 'unknown',
            'suggestions': response.get('suggestions', self.intent_classifier.get_suggestions(resolved_intent)),
            'metadata': response.get('metadata', {})
        }

    def _looks_like_work_request(self, message: str) -> bool:
        """Грубая эвристика для отличия рабочего запроса от свободного разговора."""
        message_lower = message.lower()
        work_markers = [
            'задач', 'jira', 'сотрудник', 'статист', 'сататист', 'сводк', 'смен',
            'отчет', 'отчёт', 'суп', 'логи', 'бд', 'инфра', 'роль', 'пси', 'внедрение',
            'релиз', 'ров', 'confluence', 'документ', 'ответствен', 'контрол', 'оплот',
            'покажи', 'найди', 'сгенер', 'сформир', 'передач', 'выгруз', 'предлож'
        ]
        return any(marker in message_lower for marker in work_markers)

    def _looks_like_casual_chat(self, message: str) -> bool:
        message_lower = self._normalize_command_text(message)
        casual_markers = [
            "как дела", "как ты", "поговор", "поболта", "расскажи",
            "что думаешь", "мнение", "помоги подумать", "обсудим",
            "привет", "спасибо", "класс", "здорово",
        ]
        return any(marker in message_lower for marker in casual_markers)

    def _handle_release_agent_command(self, message: str, session: ChatContext, dashboard_context: Dict = None) -> Optional[Dict]:
        normalized = self._normalize_command_text(message)
        if not normalized:
            return None

        pending = session.active_release_flow or {}
        if pending.get("type") == "release_document_flow":
            return self._handle_release_document_flow_reply(message, session)
        if pending.get("type") == "week_responsible_recommendations":
            return self._handle_week_recommendation_reply(message, session)
        if pending.get("type") == "release_week_assignee":
            return self._handle_release_week_assignee_reply(message, session)
        if pending.get("type") == "statistics_flow":
            return self._handle_statistics_flow_reply(message, session, dashboard_context)
        if pending.get("type") == "release_psi_instruction":
            if normalized in {"отмена", "отмени", "стоп", "не надо", "cancel"}:
                session.active_release_flow = None
                return {
                    "text": "Ок, поиск инструкции ПСИ отменил.",
                    "intent": "release_psi_instruction",
                    "suggestions": self._release_work_suggestions(),
                    "metadata": {"type": "release_psi_instruction", "state": "cancelled"},
                }
            release_key = self._extract_release_key(message)
            if release_key:
                return self._handle_psi_jenkins_instruction_query(
                    f"инструкция ПСИ по {release_key}",
                    session=session,
                )
            return {
                "text": "Нужен номер релиза. Пришли его одним сообщением, например `SMECLM-37025`, и я найду инструкцию ПСИ.",
                "intent": "release_psi_instruction",
                "suggestions": ["Отмена"],
                "metadata": {"type": "release_psi_instruction", "state": "need_release_key"},
            }
        if pending.get("type") == "release_week_query_result" and self._is_batch_document_request(normalized):
            return self._start_release_document_batch(session)

        if self._is_generic_statistics_prompt(normalized):
            return self._ask_statistics_type(session, message)

        if self._is_psi_jenkins_instruction_query(normalized):
            return self._handle_psi_jenkins_instruction_query(message, session=session)

        if self._is_release_document_query(normalized):
            return self._handle_release_document_query(message, session)

        if self._is_release_confluence_export_query(normalized):
            return self._handle_release_confluence_export_query()

        if self._is_release_week_recommendation_query(normalized):
            return self._handle_release_week_recommendations(session=session)

        if self._is_release_week_assignee_query(normalized):
            return self._handle_release_week_assignee_query(message, session=session)

        if self._is_release_week_control_query(normalized):
            return self._handle_release_week_control()

        if self._is_current_week_release_report_query(normalized):
            return self._handle_current_week_release_report()

        if self._is_release_statistics_query(normalized):
            return self._handle_release_statistics({}, dashboard_context, message)

        if self._is_generic_task_search_prompt(normalized):
            return {
                "text": "Что ищем по задачам? Напиши ключ Jira или пару слов для поиска, например: `Найди задачу OPLOT-12345` или `Найди задачи по логам`.",
                "intent": "search_tasks",
                "suggestions": ["Найди задачу OPLOT-12345", "Найди задачи по логам", "Сводка дневной смены"],
                "metadata": {"type": "search_tasks", "reason": "missing_query"},
            }

        return None

    def _normalize_command_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("ё", "е"))

    def _is_shift_handover_query(self, normalized: str) -> bool:
        if not normalized:
            return False
        has_shift_marker = any(marker in normalized for marker in ("смен", "дежурн"))
        has_handover_marker = any(marker in normalized for marker in (
            "сводк",
            "передач",
            "дневн",
            "вечерн",
            "handover",
        ))
        return has_shift_marker and has_handover_marker

    def _handle_shift_handover_shortcut(self, message: str, dashboard_context: Dict = None) -> Optional[Dict]:
        normalized = self._normalize_command_text(message)
        if not self._is_shift_handover_query(normalized):
            return None
        response = self._handle_shift_handover({}, dashboard_context, message)
        response.setdefault('intent', IntentType.GENERATE_REPORT.value)
        response.setdefault('suggestions', [
            "Сводка вечерней смены",
            "Найди задачу OPLOT-12345",
            "Что ты умеешь",
        ])
        metadata = dict(response.get('metadata') or {})
        metadata.setdefault('type', 'shift_handover')
        metadata.setdefault('deterministic_shortcut', True)
        response['metadata'] = metadata
        return response

    def _is_release_week_assignee_query(self, normalized: str) -> bool:
        if any(marker in normalized for marker in ("предлож", "порекоменду", "рекоменд")):
            return False
        has_person_hint = bool(
            re.search(r"\b(?:за|у|для)\s+[а-яёa-z-]{3,}", normalized, re.IGNORECASE)
            or re.search(r"\b[а-яёa-z-]{4,}(?:а|у|ым|им|ой|ого|ему)?\s+релиз", normalized, re.IGNORECASE)
        )
        if (
            "релиз" in normalized
            and has_person_hint
            and any(marker in normalized for marker in ("недел", "текущ", "закреп", "назнач", "ответствен"))
            and not any(marker in normalized for marker in ("документ", "доки", "документац", "статист", "отчет", "отчёт", "контрол"))
        ):
            return True
        return (
            "релиз" in normalized
            and any(marker in normalized for marker in ("закреп", "ответствен", "назнач"))
            and (
                "недел" in normalized
                or re.search(r"\bза\s+[а-яёa-z-]{3,}", normalized, re.IGNORECASE)
                or re.search(r"\bу\s+[а-яёa-z-]{3,}", normalized, re.IGNORECASE)
            )
        )

    def _is_release_document_query(self, normalized: str) -> bool:
        has_release_key = bool(self._extract_release_key(normalized))
        return (
            (
                any(marker in normalized for marker in ("документ", "доки", "документац"))
                and (has_release_key or "релиз" in normalized)
            )
            or (
                has_release_key
                and any(marker in normalized for marker in ("сформ", "созда", "оформ", "подготов", "собер", "пакет", "комплект"))
            )
        )

    def _is_psi_jenkins_instruction_query(self, normalized: str) -> bool:
        has_release_key = bool(self._extract_release_key(normalized))
        psi_marker = any(marker in normalized for marker in ("пси", "psi"))
        action_marker = any(marker in normalized for marker in (
            "инструкц",
            "джоб",
            "jenkins",
            "дженкинс",
            "раскат",
            "выкат",
            "деплой",
            "deploy",
            "параметр",
        ))
        return psi_marker and action_marker and (has_release_key or "релиз" in normalized)

    def _is_batch_document_request(self, normalized: str) -> bool:
        return (
            any(marker in normalized for marker in ("документ", "доки", "документац", "оформ", "подготов", "сформ", "собер"))
            and any(marker in normalized for marker in (
                "по всем", "все релиз", "все эти", "эти релиз", "всем релиз",
                "данным релиз", "поочеред", "по очеред", "подряд", "кажд", "их все",
            ))
        )

    def _is_release_confluence_export_query(self, normalized: str) -> bool:
        return (
            "confluence" in normalized
            and "релиз" in normalized
            and any(marker in normalized for marker in ("выгруз", "обнов", "экспорт", "синхрон"))
        )

    def _is_release_week_control_query(self, normalized: str) -> bool:
        return (
            ("контрол" in normalized and "недел" in normalized)
            or ("релиз" in normalized and "недел" in normalized and "без ответствен" in normalized)
            or ("неназнач" in normalized and "релиз" in normalized)
        )

    def _is_release_week_recommendation_query(self, normalized: str) -> bool:
        return (
            any(marker in normalized for marker in ("предлож", "порекоменду", "рекоменд"))
            and any(marker in normalized for marker in ("ответствен", "исполнител"))
        )

    def _is_current_week_release_report_query(self, normalized: str) -> bool:
        return (
            "релиз" in normalized
            and "недел" in normalized
            and any(marker in normalized for marker in ("отчет", "отчёт", "сводк", "план", "html"))
        )

    def _is_release_statistics_query(self, normalized: str) -> bool:
        return (
            "релиз" in normalized
            and any(marker in normalized for marker in ("статист", "отчет", "отчёт", "аналитик", "сводк"))
        )

    def _is_generic_statistics_prompt(self, normalized: str) -> bool:
        if not normalized:
            return False
        has_statistics_marker = any(marker in normalized for marker in ("статист", "сататист", "отчет", "отчёт"))
        if not has_statistics_marker:
            return False
        if any(marker in normalized for marker in (
            "релиз", "ров", "перераскат", "hotfix", "хотфикс",
            "сотрудник", "исполнител", "ответствен", "jira", "жира", "задач",
            "смен", "дежурн", "confluence", "пси", "документ",
        )):
            return False
        return len(normalized.split()) <= 8

    def _detect_statistics_kind(self, normalized: str) -> str:
        if any(marker in normalized for marker in ("релиз", "ров", "перераскат", "хотфикс", "hotfix")):
            return "release"
        if any(marker in normalized for marker in ("сотрудник", "исполнител", "jira", "жира", "задач", "закрыт")):
            return "assignee"
        return ""

    def _statistics_type_suggestions(self) -> List[str]:
        return [
            "Статистика по релизам",
            "Статистика по сотрудникам Jira",
        ]

    def _statistics_period_suggestions(self) -> List[str]:
        now = datetime.now()
        quarter = (now.month - 1) // 3 + 1
        return [
            "За текущий год",
            "За 30 дней",
            f"За {quarter} квартал {now.year}",
        ]

    def _statistics_params_from_period_text(self, message: str) -> Dict:
        params = self.intent_classifier.extract_report_params(message)
        normalized = self._normalize_command_text(message)
        if not params.get("days") and not params.get("quarter"):
            if "текущ" in normalized and "квартал" in normalized:
                now = datetime.now()
                params["quarter"] = (now.month - 1) // 3 + 1
                params["year"] = now.year
            elif "текущ" in normalized and "год" in normalized:
                params["year"] = datetime.now().year
            elif "недел" in normalized:
                params["days"] = 7
            elif "месяц" in normalized:
                params["days"] = 30
            elif "сут" in normalized or "день" in normalized:
                params["days"] = 1
        return params

    def _ask_statistics_type(self, session: Optional[ChatContext] = None, original_message: str = "") -> Dict:
        if session is not None:
            session.active_release_flow = {
                "type": "statistics_flow",
                "state": "need_type",
                "initial_period_params": self._statistics_params_from_period_text(original_message) if original_message else {},
                "created_at": datetime.now().isoformat(),
            }
        return {
            "text": (
                "Какую статистику сформировать: по релизам или по сотрудникам из Jira?"
            ),
            "intent": "statistics_clarification",
            "suggestions": self._statistics_type_suggestions(),
            "metadata": {"type": "statistics_flow", "state": "need_type"},
        }

    def _ask_statistics_period(self, session: ChatContext, kind: str) -> Dict:
        session.active_release_flow = {
            "type": "statistics_flow",
            "state": "need_period",
            "kind": kind,
            "created_at": datetime.now().isoformat(),
        }
        label = "релизам" if kind == "release" else "сотрудникам Jira"
        return {
            "text": f"За какой период сформировать статистику по {label}?",
            "intent": "statistics_clarification",
            "suggestions": self._statistics_period_suggestions(),
            "metadata": {"type": "statistics_flow", "state": "need_period", "kind": kind},
        }

    def _handle_statistics_flow_reply(self, message: str, session: ChatContext, dashboard_context: Dict = None) -> Dict:
        normalized = self._normalize_command_text(message)
        if normalized in {"отмена", "отмени", "стоп", "не надо", "cancel"}:
            session.active_release_flow = None
            return {
                "text": "Ок, формирование статистики отменил.",
                "intent": "statistics_clarification",
                "suggestions": self.get_default_suggestions(),
                "metadata": {"type": "statistics_flow", "state": "cancelled"},
            }

        flow = session.active_release_flow or {}
        state = flow.get("state")

        if state == "need_type":
            kind = self._detect_statistics_kind(normalized)
            if not kind:
                return {
                    "text": "Уточни тип статистики: по релизам или по сотрудникам из Jira?",
                    "intent": "statistics_clarification",
                    "suggestions": self._statistics_type_suggestions(),
                    "metadata": {"type": "statistics_flow", "state": "need_type"},
                }

            params = self._statistics_params_from_period_text(message)
            if not (params.get("days") or params.get("quarter")):
                params.update(flow.get("initial_period_params") or {})
            has_period = bool(params.get("days") or params.get("quarter"))
            if has_period:
                session.active_release_flow = None
                command_text = f"статистика по {'релизам' if kind == 'release' else 'сотрудникам Jira'} {message}"
                if kind == "release":
                    return self._handle_release_statistics(params, dashboard_context, command_text)
                return self._handle_assignee_statistics(params)

            return self._ask_statistics_period(session, kind)

        if state == "need_period":
            kind = flow.get("kind") or self._detect_statistics_kind(normalized)
            if not kind:
                session.active_release_flow = None
                return self._ask_statistics_type(session)

            params = self._statistics_params_from_period_text(message)
            session.active_release_flow = None
            command_text = f"статистика по {'релизам' if kind == 'release' else 'сотрудникам Jira'} {message}"
            if kind == "release":
                return self._handle_release_statistics(params, dashboard_context, command_text)
            return self._handle_assignee_statistics(params)

        session.active_release_flow = None
        return self._ask_statistics_type(session)

    def _is_generic_task_search_prompt(self, normalized: str) -> bool:
        return normalized in {"поиск задач", "найти задачи", "показать задачи", "задачи"} or (
            "задач" in normalized
            and any(marker in normalized for marker in ("поиск", "найди", "найти", "показать"))
            and not re.search(r"\b[A-ZА-Я]+-\d+\b", normalized, re.IGNORECASE)
            and len(normalized.split()) <= 3
        )

    def _extract_release_key(self, value: str) -> str:
        match = re.search(r"\b([A-ZА-Я]+-\d+)\b", str(value or ""), re.IGNORECASE)
        return match.group(1).upper() if match else ""

    def _parse_release_date(self, value: Any) -> Optional[datetime]:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except Exception:
                continue
        return None

    def _current_week_bounds(self) -> Tuple[datetime, datetime]:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        return week_start, week_end

    def _release_start_dt(self, item: Dict) -> Optional[datetime]:
        return (
            self._parse_release_date(item.get("deployment_start_iso"))
            or self._parse_release_date(item.get("deployment_start"))
            or self._parse_release_date(item.get("source_deployment_start_iso"))
            or self._parse_release_date(item.get("source_deployment_start"))
        )

    def _release_is_current_week(self, item: Dict, week_start: datetime, week_end: datetime) -> bool:
        start_dt = self._release_start_dt(item)
        return bool(start_dt and week_start <= start_dt <= week_end)

    def _release_doc_date(self, item: Dict) -> str:
        start_dt = self._release_start_dt(item)
        if start_dt:
            return start_dt.strftime("%d.%m.%Y")
        value = str(item.get("deployment_start") or "").strip()
        return value.split(" ", 1)[0] if value else ""

    def _is_yes(self, normalized: str) -> bool:
        return normalized in {"да", "ага", "угу", "ок", "yes", "y"} or normalized.startswith("да ") or any(
            marker in normalized
            for marker in ("используем", "подтверждаю", "создавай", "создать", "верно", "соглас")
        )

    def _is_no(self, normalized: str) -> bool:
        return normalized in {"нет", "не", "no", "n"} or normalized.startswith("нет") or any(
            marker in normalized
            for marker in ("не созда", "не надо", "без зни", "друг", "свою", "свой", "измен", "не использу")
        )

    def _parse_playbooks(self, message: str) -> List[str]:
        if any(marker in self._normalize_command_text(message) for marker in ("без плейбук", "плейбуки не нужны", "нет плейбук")):
            return []
        parts = re.split(r"[\n;,]+", str(message or ""))
        return [part.strip(" -\t") for part in parts if part.strip(" -\t")]

    def _save_release_zip_for_chat(self, release_key: str, zip_buffer) -> str:
        os.makedirs(RELEASE_DOCS_DIR, exist_ok=True)
        cleanup_old_release_documents()
        document_id = f"{release_key}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        path = get_release_document_path(document_id)
        with open(path, "wb") as file:
            file.write(zip_buffer.getvalue())
        return document_id

    def _extract_responsible_surname_query(self, message: str, items: List[Dict]) -> str:
        normalized = self._normalize_command_text(message)
        surnames = set()
        for item in items:
            responsibles = item.get("psi_responsibles") or []
            if not isinstance(responsibles, list):
                responsibles = [responsibles] if responsibles else []
            for responsible in responsibles:
                surname = self._normalize_command_text(str(responsible).split()[0] if responsible else "")
                if len(surname) >= 4:
                    surnames.add(surname)

        for surname in sorted(surnames, key=len, reverse=True):
            if re.search(rf"\b{re.escape(surname)}\w*\b", normalized):
                return surname

        fallback_patterns = [
            r"\bза\s+([а-яёa-z-]{3,})\w*\b",
            r"\bу\s+([а-яёa-z-]{3,})\w*\b",
            r"\bдля\s+([а-яёa-z-]{3,})\w*\b",
        ]
        stop_words = {
            "кем", "кого", "кому", "ним", "ней", "недел", "релиз", "ответствен",
            "закреплен", "закреплены", "назначен", "назначены",
        }
        for pattern in fallback_patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                candidate = self._normalize_command_text(match.group(1))
                if candidate and candidate not in stop_words:
                    return candidate
        return ""

    def _extract_plain_surname_reply(self, message: str) -> str:
        normalized = self._normalize_command_text(message)
        if not normalized:
            return ""
        blocked_markers = {
            "релиз", "недел", "ответствен", "закреп", "назнач", "документ", "статист",
            "отчет", "отчёт", "контрол", "задач", "пси", "инструкц", "confluence",
        }
        if any(marker in normalized for marker in blocked_markers):
            return ""
        tokens = re.findall(r"[а-яёa-z-]{3,}", normalized, re.IGNORECASE)
        if 1 <= len(tokens) <= 2:
            return tokens[0]
        return ""

    def _responsible_matches_surname(self, item: Dict, surname_query: str) -> bool:
        responsibles = item.get("psi_responsibles") or []
        if not isinstance(responsibles, list):
            responsibles = [responsibles] if responsibles else []
        for responsible in responsibles:
            surname = self._normalize_command_text(str(responsible).split()[0] if responsible else "")
            if surname and (
                surname == surname_query
                or surname.startswith(surname_query)
                or surname_query.startswith(surname)
                or self._surname_case_stem_matches(surname, surname_query)
            ):
                return True
        return False

    def _match_oplot_name(self, value: str) -> str:
        normalized = self._normalize_command_text(value)
        if not normalized:
            return ""

        by_normalized = {self._normalize_command_text(name): name for name in OPLOT_VALUES}
        if normalized in by_normalized:
            return by_normalized[normalized]

        for name in OPLOT_VALUES:
            name_normalized = self._normalize_command_text(name)
            surname = self._normalize_command_text(name.split()[0] if name else "")
            if not surname:
                continue
            if (
                surname == normalized
                or surname in normalized
                or normalized in name_normalized
                or self._surname_case_stem_matches(surname, normalized)
            ):
                return name
        return ""

    def _get_release_row_by_row_key(self, row_key: str) -> Optional[Dict]:
        snapshot = get_release_monitor_snapshot() or {}
        items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
        row_key = str(row_key or "").strip()
        if not row_key:
            return None
        return next(
            (item for item in items if str(item.get("row_key") or item.get("release_key") or "").strip() == row_key),
            None,
        )

    def _is_release_document_batch_candidate(self, item: Dict) -> bool:
        row_state = str((item or {}).get("row_state") or "").strip().lower()
        if (item or {}).get("is_cancelled") or row_state == "cancelled":
            return False

        is_reroll = bool((item or {}).get("is_reroll"))
        if is_reroll and row_state in {"planned", "today"}:
            return True

        if (item or {}).get("is_final") or row_state == "final":
            return False

        return True

    def _save_release_assignment_from_item(
        self,
        item: Dict,
        *,
        checker: Optional[str] = None,
        responsibles: Optional[List[str]] = None,
    ) -> Dict:
        row_key = str(item.get("row_key") or item.get("release_key") or "").strip()
        if not row_key:
            raise ValueError("Не найден ключ строки релиза")
        reviewer = str(item.get("psi_owner") or "").strip()
        reviewer_source = str(item.get("psi_owner_source") or "").strip() or None
        current_checker = str(item.get("psi_checker") or "").strip()
        current_responsibles = item.get("psi_responsibles") or []
        if not isinstance(current_responsibles, list):
            current_responsibles = [current_responsibles] if current_responsibles else []

        return set_release_monitor_assignment(
            row_key,
            reviewer,
            checker if checker is not None else current_checker,
            responsibles if responsibles is not None else current_responsibles,
            reviewer_source=reviewer_source,
        )

    def _surname_case_stem_matches(self, surname: str, query: str) -> bool:
        surname = self._normalize_command_text(surname)
        query = self._normalize_command_text(query)
        if len(surname) < 4 or len(query) < 4:
            return False
        endings = (
            "ым", "им", "ом", "ем", "ой", "ей", "ого", "его", "ому", "ему",
            "а", "я", "у", "ю", "е", "ы", "и",
        )
        variants = {query}
        for ending in endings:
            if query.endswith(ending) and len(query) > len(ending) + 2:
                variants.add(query[:-len(ending)])
        return any(
            len(variant) >= 4
            and (
                surname.startswith(variant)
                or self._surname_fuzzy_matches(surname, variant)
            )
            for variant in variants
        )

    def _surname_fuzzy_matches(self, surname: str, query: str) -> bool:
        surname = self._normalize_command_text(surname)
        query = self._normalize_command_text(query)
        if len(surname) < 5 or len(query) < 5:
            return False
        if surname[:1] != query[:1] or abs(len(surname) - len(query)) > 2:
            return False

        previous = list(range(len(query) + 1))
        for left_index, left_char in enumerate(surname, 1):
            current = [left_index]
            for right_index, right_char in enumerate(query, 1):
                current.append(min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                ))
            previous = current
        return previous[-1] <= 2

    def _format_release_link(self, release_key: str, release_url: str = "") -> str:
        release_key = str(release_key or "").strip() or "-"
        release_url = str(release_url or "").strip()
        return f"[{release_key}]({release_url})" if release_url else release_key

    def _handle_release_week_control(self) -> Dict:
        try:
            control = get_release_monitor_week_control() or {}
            stats = control.get("statistics", {}) if isinstance(control, dict) else {}
            period = control.get("period", {}) if isinstance(control, dict) else {}
            missing = control.get("missing_responsible") or []
            candidates = control.get("candidates", {}) or {}
            assigned_load = control.get("assigned_load", {}) or {}

            lines = [
                "*Контроль недели по релизам*",
                f"Период: {period.get('label', '-')}",
                "",
                f"Релизы недели: {stats.get('week_releases', 0)}",
                f"Без ответственного: {stats.get('missing_responsible', 0)}",
                f"Доступных кандидатов: {stats.get('available_candidates', 0)}",
                f"Исключено по графику: {stats.get('excluded_candidates', 0)}",
            ]

            if assigned_load:
                load_preview = ", ".join(f"{name}: {count}" for name, count in list(assigned_load.items())[:6])
                lines.extend(["", f"Текущая нагрузка: {load_preview}"])

            if missing:
                lines.extend(["", "*Релизы без ответственного:*"])
                for item in missing[:8]:
                    release_link = self._format_release_link(item.get("release_key"), item.get("release_url"))
                    rov_key = str(item.get("rov_key") or "без РОВ").strip()
                    start = str(item.get("deployment_start") or "-").strip()
                    summary = str(item.get("release_summary") or item.get("system_name") or "").strip()
                    summary_text = f" - {summary[:90]}" if summary else ""
                    lines.append(f"• {release_link} / {rov_key} - {start}{summary_text}")
                if len(missing) > 8:
                    lines.append(f"• ... и еще {len(missing) - 8}")
            else:
                lines.extend(["", "По релизам текущей недели все ответственные назначены."])

            available_names = [item.get("name") for item in candidates.get("available", []) if item.get("name")]
            if available_names:
                lines.extend(["", f"Доступны для назначения: {', '.join(available_names[:10])}"])

            return {
                "text": "\n".join(lines),
                "intent": "release_week_control",
                "suggestions": ["Предложи ответственных по релизам недели", "Релизы недели по ответственному", "Выгрузить таблицу релизов в Confluence"],
                "metadata": {"type": "release_week_control", "control": control},
            }
        except Exception as exc:
            logging.error("Release week control from chat failed: %s", exc)
            return {
                "text": f"Не удалось собрать контроль недели: {exc}",
                "intent": "release_week_control",
                "suggestions": self._release_work_suggestions(),
                "metadata": {"type": "release_week_control", "error": str(exc)},
            }

    def _handle_release_week_recommendations(self, session: Optional[ChatContext] = None) -> Dict:
        try:
            result = get_release_monitor_week_responsible_recommendations() or {}
            control = result.get("control", {}) or {}
            recommendations = result.get("recommendations") or []
            message = str(result.get("message") or "").strip()
            summary = str(result.get("summary") or "").strip()
            period = (control.get("period") or {}).get("label", "-")

            lines = ["*Рекомендации по ответственным на неделю*", f"Период: {period}", ""]
            if summary:
                lines.append(summary)
                lines.append("")
            if message and not recommendations:
                lines.append(message)

            if recommendations:
                for index, item in enumerate(recommendations[:10], 1):
                    release_key = str(item.get("release_key") or "-").strip()
                    rov_key = str(item.get("rov_key") or "без РОВ").strip()
                    responsible = str(item.get("recommended") or item.get("responsible") or "-").strip()
                    reason = str(item.get("reason") or "").strip()
                    reason_text = f" - {reason}" if reason else ""
                    lines.append(f"{index}. {release_key} / {rov_key}: {responsible}{reason_text}")
                if len(recommendations) > 10:
                    lines.append(f"... и еще {len(recommendations) - 10}")
            elif not message:
                lines.append("GigaChat не вернул кандидатов. Проверь, есть ли релизы без ответственного и доступные сотрудники по графику.")

            if recommendations and session is not None:
                session.active_release_flow = {
                    "type": "week_responsible_recommendations",
                    "state": "confirm",
                    "recommendations": recommendations,
                    "created_at": datetime.now().isoformat(),
                }
                lines.extend([
                    "",
                    "Могу применить назначения в таблицу. Напиши `применить все`, или пришли правки в формате:",
                    "`EMRM-12345 Кашкин С.Н.; SMECSC-12345 Гапоненко Д.А.; остальные ок`.",
                ])

            return {
                "text": "\n".join(lines),
                "intent": "release_week_recommendations",
                "suggestions": (
                    ["Применить все", "Внести правки", "Отмена"]
                    if recommendations
                    else ["Контроль недели", "Релизы недели по ответственному", "Сформировать документы по релизу"]
                ),
                "metadata": {"type": "release_week_recommendations", "recommendation": result},
            }
        except Exception as exc:
            logging.error("Release week recommendations from chat failed: %s", exc)
            return {
                "text": f"Не удалось получить рекомендации по ответственным: {exc}",
                "intent": "release_week_recommendations",
                "suggestions": self._release_work_suggestions(),
                "metadata": {"type": "release_week_recommendations", "error": str(exc)},
            }

    def _is_apply_week_recommendation_command(self, normalized: str) -> bool:
        return (
            normalized in {"да", "ок", "ага", "согласен", "согласна", "принять", "применить", "назначить"}
            or any(marker in normalized for marker in ("применить все", "прими все", "назначь всех", "назначай", "все ок", "остальные ок", "остальное ок"))
        )

    def _format_week_recommendation_plan(self, recommendations: List[Dict]) -> str:
        if not recommendations:
            return "Активных рекомендаций не осталось."
        lines = []
        for index, item in enumerate(recommendations, 1):
            release_key = str(item.get("release_key") or "-").strip()
            rov_key = str(item.get("rov_key") or "без РОВ").strip()
            responsible = str(item.get("recommended") or "-").strip()
            reason = str(item.get("reason") or "").strip()
            reason_text = f" - {reason}" if reason else ""
            lines.append(f"{index}. {release_key} / {rov_key}: {responsible}{reason_text}")
        return "\n".join(lines)

    def _find_recommendation_for_text(self, recommendations: List[Dict], text: str) -> Optional[Dict]:
        normalized = self._normalize_command_text(text)
        release_key = self._extract_release_key(text)
        if release_key:
            matches = [
                item for item in recommendations
                if str(item.get("release_key") or "").strip().upper() == release_key
                or str(item.get("row_key") or "").strip().upper() == release_key
            ]
            if matches:
                return matches[0]

        number_match = re.search(r"^\s*(\d{1,2})(?:[.)]|\s)", normalized)
        if number_match:
            index = int(number_match.group(1)) - 1
            if 0 <= index < len(recommendations):
                return recommendations[index]

        if len(recommendations) == 1:
            return recommendations[0]
        return None

    def _parse_week_recommendation_edits(self, message: str, recommendations: List[Dict]) -> Tuple[Dict[str, str], set, List[str]]:
        edits: Dict[str, str] = {}
        skipped = set()
        unknown_parts: List[str] = []
        parts = [
            part.strip()
            for part in re.split(r"[\n;]+", str(message or ""))
            if part.strip()
        ]

        for part in parts:
            normalized_part = self._normalize_command_text(part)
            if any(marker in normalized_part for marker in ("остальные ок", "остальное ок", "все ок", "применить", "принять")):
                continue

            target = self._find_recommendation_for_text(recommendations, part)
            if not target:
                if self._match_oplot_name(part) and len(recommendations) == 1:
                    target = recommendations[0]
                else:
                    unknown_parts.append(part)
                    continue

            row_key = str(target.get("row_key") or "").strip()
            if any(marker in normalized_part for marker in ("пропусти", "не назнач", "убери", "исключи")):
                if row_key:
                    skipped.add(row_key)
                continue

            name = self._match_oplot_name(part)
            if not name:
                unknown_parts.append(part)
                continue
            if row_key:
                edits[row_key] = name

        return edits, skipped, unknown_parts

    def _apply_week_recommendations(self, recommendations: List[Dict]) -> Tuple[List[str], List[str]]:
        applied_lines: List[str] = []
        errors: List[str] = []
        for item in recommendations:
            row_key = str(item.get("row_key") or "").strip()
            responsible = str(item.get("recommended") or "").strip()
            release_key = str(item.get("release_key") or row_key or "-").strip()
            rov_key = str(item.get("rov_key") or "без РОВ").strip()
            if not row_key or not responsible:
                errors.append(f"{release_key}: нет строки или ответственного")
                continue
            row_item = self._get_release_row_by_row_key(row_key)
            if not row_item:
                errors.append(f"{release_key}: строка уже не найдена в кеше")
                continue
            current_responsibles = row_item.get("psi_responsibles") or []
            if not isinstance(current_responsibles, list):
                current_responsibles = [current_responsibles] if current_responsibles else []
            current_responsibles = [str(value or "").strip() for value in current_responsibles if str(value or "").strip()]
            next_responsibles = (
                current_responsibles
                if responsible in current_responsibles
                else [responsible, *current_responsibles]
            )
            try:
                self._save_release_assignment_from_item(row_item, responsibles=next_responsibles)
                applied_lines.append(f"{release_key} / {rov_key}: {responsible}")
            except Exception as exc:
                errors.append(f"{release_key}: {exc}")
        return applied_lines, errors

    def _handle_week_recommendation_reply(self, message: str, session: ChatContext) -> Dict:
        flow = session.active_release_flow or {}
        recommendations = flow.get("recommendations") or []
        normalized = self._normalize_command_text(message)

        if "отмена" in normalized or "сброс" in normalized:
            session.active_release_flow = None
            return {
                "text": "Ок, не применяю рекомендации по ответственным.",
                "intent": "release_week_recommendations",
                "suggestions": ["Контроль недели", "Предложи ответственных по релизам недели"],
                "metadata": {"type": "release_week_recommendations", "state": "cancelled"},
            }

        if not recommendations:
            session.active_release_flow = None
            return {
                "text": "Список рекомендаций пуст. Можно заново запросить `Предложи ответственных по релизам недели`.",
                "intent": "release_week_recommendations",
                "suggestions": ["Предложи ответственных по релизам недели", "Контроль недели"],
                "metadata": {"type": "release_week_recommendations", "state": "empty"},
            }

        if normalized in {"внести правки", "правки", "изменить"}:
            return {
                "text": (
                    "Напиши правки одной строкой или списком. Например:\n"
                    "`EMRM-12345 Кашкин С.Н.; SMECSC-12345 Гапоненко Д.А.; остальные ок`.\n\n"
                    "Если текущий вариант подходит, напиши `применить все`."
                ),
                "intent": "release_week_recommendations",
                "suggestions": ["Применить все", "Отмена"],
                "metadata": {"type": "release_week_recommendations", "state": "awaiting_edits"},
            }

        edits, skipped, unknown_parts = self._parse_week_recommendation_edits(message, recommendations)
        if unknown_parts and not edits and not skipped and not self._is_apply_week_recommendation_command(normalized):
            return {
                "text": (
                    "Не смог уверенно разобрать правки.\n\n"
                    "Формат: `ключ релиза + ФИО`, например `EMRM-12345 Кашкин С.Н.`. "
                    "Или напиши `применить все`."
                ),
                "intent": "release_week_recommendations",
                "suggestions": ["Применить все", "Внести правки", "Отмена"],
                "metadata": {"type": "release_week_recommendations", "state": "edit_parse_error", "unknown": unknown_parts},
            }

        if edits or skipped:
            updated = []
            for item in recommendations:
                row_key = str(item.get("row_key") or "").strip()
                if row_key in skipped:
                    continue
                next_item = dict(item)
                if row_key in edits:
                    next_item["recommended"] = edits[row_key]
                    next_item["reason"] = "ручная правка через чат"
                updated.append(next_item)
            flow["recommendations"] = updated
            recommendations = updated

            if not self._is_apply_week_recommendation_command(normalized):
                return {
                    "text": (
                        "Принял правки. Итоговый план сейчас такой:\n\n"
                        f"{self._format_week_recommendation_plan(recommendations)}\n\n"
                        "Применить эти назначения в таблицу?"
                    ),
                    "intent": "release_week_recommendations",
                    "suggestions": ["Применить все", "Внести правки", "Отмена"],
                    "metadata": {"type": "release_week_recommendations", "state": "confirm_after_edits", "recommendations": recommendations},
                }

        if self._is_apply_week_recommendation_command(normalized) or edits or skipped:
            applied, errors = self._apply_week_recommendations(recommendations)
            session.active_release_flow = None
            lines = ["Готово, применил назначения в таблицу."]
            if applied:
                lines.extend(["", "*Назначено:*", *[f"• {line}" for line in applied]])
            if errors:
                lines.extend(["", "*Не удалось применить:*", *[f"• {line}" for line in errors]])
            return {
                "text": "\n".join(lines),
                "intent": "release_week_recommendations",
                "suggestions": ["Контроль недели", "Релизы недели по ответственному", "Сформировать документы по релизу"],
                "metadata": {"type": "release_week_recommendations", "state": "applied", "applied": applied, "errors": errors},
            }

        return {
            "text": (
                "У меня есть неподтвержденный план назначений:\n\n"
                f"{self._format_week_recommendation_plan(recommendations)}\n\n"
                "Напиши `применить все`, `внести правки` или `отмена`."
            ),
            "intent": "release_week_recommendations",
            "suggestions": ["Применить все", "Внести правки", "Отмена"],
            "metadata": {"type": "release_week_recommendations", "state": "confirm", "recommendations": recommendations},
        }

    def _handle_current_week_release_report(self) -> Dict:
        try:
            snapshot = get_release_monitor_snapshot() or {}
            items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
            if not items:
                return {
                    "text": "В таблице релизов пока нет данных для отчета текущей недели.",
                    "intent": "release_current_week_report",
                    "suggestions": self._release_work_suggestions(),
                    "metadata": {"type": "release_current_week_report", "count": 0},
                }
            report_service = get_release_report_service()
            report_data = report_service.generate_current_week_plan_report(items)
            html_content = report_service.generate_current_week_plan_html(report_data)
            report_id = save_report_to_disk(html_content)
            download_url = f"/dashboard/api/chat/report/download/{report_id}"
            stats = report_data.get("statistics", {})
            period = report_data.get("period", {})
            try:
                control_missing = (get_release_monitor_week_control() or {}).get("statistics", {}).get("missing_responsible", 0)
            except Exception:
                control_missing = 0
            return {
                "text": (
                    "*План релизов текущей недели готов*\n"
                    f"Период: {period.get('label', '-')}\n\n"
                    f"Строк в отчете: {stats.get('total', 0)}\n"
                    f"Без ответственного: {control_missing}\n\n"
                    f"[Скачать HTML-отчет]({download_url})"
                ),
                "intent": "release_current_week_report",
                "suggestions": ["Контроль недели", "Предложи ответственных по релизам недели", "Выгрузить таблицу релизов в Confluence"],
                "metadata": {"type": "release_current_week_report", "download_url": download_url, "report_id": report_id},
            }
        except Exception as exc:
            logging.error("Current week release report from chat failed: %s", exc)
            return {
                "text": f"Не удалось сформировать отчет текущей недели: {exc}",
                "intent": "release_current_week_report",
                "suggestions": self._release_work_suggestions(),
                "metadata": {"type": "release_current_week_report", "error": str(exc)},
            }

    def _handle_release_week_assignee_query(self, message: str, session: Optional[ChatContext] = None) -> Dict:
        snapshot = get_release_monitor_snapshot() or {}
        items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
        week_start, week_end = self._current_week_bounds()
        surname_query = self._extract_responsible_surname_query(message, items) or self._extract_plain_surname_reply(message)

        if not surname_query:
            if session is not None:
                session.active_release_flow = {
                    "type": "release_week_assignee",
                    "state": "need_surname",
                    "created_at": datetime.now().isoformat(),
                }
            return {
                "text": "По кому показать релизы текущей недели? Пришли фамилию одним сообщением, например `Иванов`.",
                "intent": "release_week_query",
                "suggestions": ["Отмена"],
                "metadata": {"type": "release_week_query", "reason": "missing_surname"},
            }

        matched_items = [
            item for item in items
            if self._release_is_current_week(item, week_start, week_end)
            and self._responsible_matches_surname(item, surname_query)
        ]
        matched_items.sort(key=lambda item: self._release_start_dt(item) or datetime.min)

        display_surname = surname_query[:1].upper() + surname_query[1:]
        if not matched_items:
            return {
                "text": (
                    f"На текущей неделе ({week_start.strftime('%d.%m.%Y')} - {week_end.strftime('%d.%m.%Y')}) "
                    f"я не нашел релизов, закрепленных за {display_surname}."
                ),
                "intent": "release_week_query",
                "suggestions": ["Показать релизы недели по ответственному", "Сформировать документы по релизу"],
                "metadata": {"type": "release_week_query", "count": 0},
            }

        lines = [
            f"*Релизы текущей недели за {display_surname}*",
            f"Период: {week_start.strftime('%d.%m.%Y')} - {week_end.strftime('%d.%m.%Y')}",
            "",
        ]
        for index, item in enumerate(matched_items, 1):
            release_key = str(item.get("release_key") or "").strip() or "-"
            rov_key = str(item.get("rov_key") or "").strip()
            release_url = str(item.get("release_url") or "").strip()
            rov_url = str(item.get("rov_url") or "").strip()
            start = str(item.get("deployment_start") or "").strip() or "-"
            status = str(item.get("release_status") or "").strip() or "-"
            summary = str(item.get("release_summary") or item.get("system_name") or "").strip()
            release_link = f"[{release_key}]({release_url})" if release_url else release_key
            rov_link = f"[{rov_key}]({rov_url})" if rov_key and rov_url else (rov_key or "без РОВ")
            summary_text = f" - {summary[:90]}" if summary else ""
            lines.append(f"{index}. {release_link} / {rov_link} - {start} - {status}{summary_text}")

        first_key = str(matched_items[0].get("release_key") or "").strip()
        release_suggestions = []
        seen_release_keys = set()
        for suggestion_item in matched_items:
            suggestion_key = str(suggestion_item.get("release_key") or "").strip()
            if suggestion_key and suggestion_key not in seen_release_keys:
                seen_release_keys.add(suggestion_key)
                release_suggestions.append(f"Сформировать документы по {suggestion_key}")
            if len(release_suggestions) >= 6:
                break
        suggestions = release_suggestions + ["Подготовить документы по всем этим релизам", "Выгрузить таблицу релизов в Confluence", "Контроль недели", "Что ты умеешь"]
        if session is not None:
            session.active_release_flow = {
                "type": "release_week_query_result",
                "state": "ready_for_batch_documents",
                "items": [
                    {
                        "row_key": item.get("row_key") or item.get("release_key") or "",
                        "release_key": item.get("release_key", ""),
                        "rov_key": item.get("rov_key", ""),
                        "deployment_start": item.get("deployment_start", ""),
                    }
                    for item in matched_items
                ],
                "created_at": datetime.now().isoformat(),
            }

        return {
            "text": "\n".join(lines),
            "intent": "release_week_query",
            "suggestions": suggestions,
            "metadata": {
                "type": "release_week_query",
                "count": len(matched_items),
                "items": [
                    {
                        "row_key": item.get("row_key", ""),
                        "release_key": item.get("release_key", ""),
                        "rov_key": item.get("rov_key", ""),
                        "deployment_start": item.get("deployment_start", ""),
                    }
                    for item in matched_items[:20]
                ],
            },
        }

    def _handle_release_week_assignee_reply(self, message: str, session: ChatContext) -> Dict:
        normalized = self._normalize_command_text(message)
        if normalized in {"отмена", "отмени", "стоп", "не надо", "cancel"}:
            session.active_release_flow = None
            return {
                "text": "Ок, поиск релизов недели по ответственному отменил.",
                "intent": "release_week_query",
                "suggestions": self._release_work_suggestions(),
                "metadata": {"type": "release_week_query", "state": "cancelled"},
            }

        snapshot = get_release_monitor_snapshot() or {}
        items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
        surname_query = self._extract_responsible_surname_query(message, items) or self._extract_plain_surname_reply(message)
        if not surname_query:
            return {
                "text": "Нужна только фамилия ответственного. Пришли ее одним сообщением, например `Иванов`.",
                "intent": "release_week_query",
                "suggestions": ["Отмена"],
                "metadata": {"type": "release_week_query", "state": "need_surname"},
            }

        session.active_release_flow = None
        return self._handle_release_week_assignee_query(f"релизы недели за {surname_query}", session=session)

    def _find_release_rows_for_key(self, release_key: str) -> List[Dict]:
        snapshot = get_release_monitor_snapshot() or {}
        items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
        release_key = str(release_key or "").strip().upper()
        matched = [item for item in items if str(item.get("release_key") or "").strip().upper() == release_key]
        week_start, week_end = self._current_week_bounds()
        matched.sort(
            key=lambda item: (
                0 if self._release_is_current_week(item, week_start, week_end) else 1,
                -((self._release_start_dt(item) or datetime(1970, 1, 1)).timestamp()),
            ),
        )
        return matched

    def _extract_release_ke_for_psi(self, item: Dict) -> str:
        for key in ("ke_id", "ke"):
            value = str((item or {}).get(key) or "").strip()
            if value:
                return value

        for line in (item or {}).get("release_name_lines") or []:
            match = re.search(r"\((?:CI)?0*(\d{5,})\)", str(line or ""), re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    def _is_psi_instruction_section_label(self, value: str) -> bool:
        section = str(value or "").strip()
        if not section:
            return False
        lowered = section.lower()
        if lowered.startswith(("http://", "https://", "config_dir", "subsystem")):
            return False
        if ":" in lowered:
            return False
        if any(marker in lowered for marker in ("hosts_group", "host_group", "hosts_to_update", "ansible", "inventory")):
            return False
        return True

    def _format_psi_instruction_match(self, match: Dict, index: int, total: int) -> str:
        prefix = f"{index}. " if total > 1 else ""
        title = str(match.get("title") or "Инструкция ПСИ").strip()
        contour = str(match.get("contour") or "").strip()
        section = str(match.get("section") or "").strip()
        url = str(match.get("jenkins_url") or "").strip()
        config_dir = str(match.get("config_dir") or "").strip()
        subsystem = str(match.get("subsystem") or "").strip()

        lines = [f"{prefix}*{title}*"]
        if self._is_psi_instruction_section_label(section):
            lines.append(f"Раздел: {section}")
        if contour:
            lines.append(f"Контур: {contour}")
        if url:
            lines.append(f"Jenkins: [{url}]({url})")
        if config_dir:
            lines.append(f"CONFIG_DIR: `{config_dir}`")
        if subsystem:
            lines.append(f"SUBSYSTEM: `{subsystem}`")
        return "\n".join(lines)

    def _handle_psi_jenkins_instruction_query(self, message: str, session: Optional[ChatContext] = None) -> Dict:
        release_key = self._extract_release_key(message)
        if not release_key:
            if session is not None:
                session.active_release_flow = {
                    "type": "release_psi_instruction",
                    "state": "need_release_key",
                    "created_at": datetime.now().isoformat(),
                }
            return {
                "text": "Нужен номер релиза. Пришли его одним сообщением, например `SMECLM-37025`, и я найду инструкцию ПСИ.",
                "intent": "release_psi_instruction",
                "suggestions": ["Отмена"],
                "metadata": {"type": "release_psi_instruction", "state": "need_release_key"},
            }

        if session is not None and (session.active_release_flow or {}).get("type") == "release_psi_instruction":
            session.active_release_flow = None

        rows = self._find_release_rows_for_key(release_key)
        if not rows:
            return {
                "text": f"Релиз *{release_key}* не найден в текущем кеше Блока релизов. Обнови Блок релизов и повтори запрос.",
                "intent": "release_psi_instruction",
                "suggestions": ["Открыть блок релизов", "Инструкция ПСИ по релизу"],
                "metadata": {"type": "release_psi_instruction", "state": "release_not_found", "release_key": release_key},
            }

        row = next((item for item in rows if self._extract_release_ke_for_psi(item)), rows[0])
        ke_value = self._extract_release_ke_for_psi(row)
        if not ke_value:
            return {
                "text": f"По *{release_key}* не нашел КЭ в кеше Блока релизов, поэтому не могу подобрать инструкцию ПСИ.",
                "intent": "release_psi_instruction",
                "suggestions": ["Открыть блок релизов", "Инструкция ПСИ по релизу"],
                "metadata": {"type": "release_psi_instruction", "state": "ke_not_found", "release_key": release_key},
            }

        try:
            result = find_psi_jenkins_instructions_by_ke(ke_value)
        except Exception as exc:
            logging.error("PSI Jenkins instruction lookup failed for %s: %s", release_key, exc)
            return {
                "text": f"Не удалось прочитать страницу с ПСИ Jenkins для *{release_key}*: {exc}",
                "intent": "release_psi_instruction",
                "suggestions": ["Повторить инструкцию ПСИ по релизу", "Открыть блок релизов"],
                "metadata": {"type": "release_psi_instruction", "state": "confluence_error", "release_key": release_key, "error": str(exc)},
            }

        matches = result.get("matches") or []
        cache = result.get("cache") or {}
        normalized_ke = result.get("ke_id") or str(ke_value)
        release_url = str(row.get("release_url") or "").strip()
        release_link = self._format_release_link(release_key, release_url)
        page_url = str(cache.get("page_url") or "").strip()

        if not matches:
            page_text = f"\n\nСтраница с инструкциями: {page_url}" if page_url else ""
            return {
                "text": (
                    f"По {release_link} нашел КЭ `{normalized_ke}`, но на странице ПСИ Jenkins инструкции для этого КЭ нет."
                    f"{page_text}"
                ),
                "intent": "release_psi_instruction",
                "suggestions": ["Сформировать документы по релизу", "Показать релизы недели по ответственному"],
                "metadata": {
                    "type": "release_psi_instruction",
                    "state": "instruction_not_found",
                    "release_key": release_key,
                    "ke_id": normalized_ke,
                    "page_version": cache.get("page_version"),
                },
            }

        rendered_matches = [
            self._format_psi_instruction_match(match, index, len(matches))
            for index, match in enumerate(matches[:10], 1)
        ]
        more_text = f"\n\nНашел больше 10 инструкций, показал первые 10 из {len(matches)}." if len(matches) > 10 else ""
        cache_note = ""
        if cache.get("cache_status") == "stale":
            cache_note = "\n\nНе смог проверить актуальную версию Confluence, показываю последнюю сохраненную копию."

        text = (
            f"*Инструкция ПСИ для {release_link}*\n"
            f"КЭ релиза: `{normalized_ke}`\n"
            f"Версия страницы Confluence: {cache.get('page_version', '-')}\n\n"
            + "\n\n".join(rendered_matches)
            + more_text
            + cache_note
        )

        return {
            "text": text,
            "intent": "release_psi_instruction",
            "suggestions": [f"Сформировать документы по {release_key}", "Показать релизы недели по ответственному", "Что ты умеешь"],
            "metadata": {
                "type": "release_psi_instruction",
                "state": "found",
                "release_key": release_key,
                "row_key": row.get("row_key") or "",
                "ke_id": normalized_ke,
                "count": len(matches),
                "page_version": cache.get("page_version"),
                "cache_status": cache.get("cache_status"),
            },
        }

    def _format_template_candidate_label(self, candidate: Dict) -> str:
        category = str(candidate.get("category") or "").strip()
        release_clean = str(candidate.get("release_clean") or candidate.get("release_full") or "").strip()
        if category and release_clean:
            return f"{category} / {release_clean}"
        return release_clean or category or "вариант шаблона"

    def _select_template_candidate(self, candidates: List[Dict], message: str) -> Optional[Dict]:
        if not candidates:
            return None

        normalized = self._normalize_command_text(message)
        number_match = re.search(r"\b(\d{1,2})\b", normalized)
        if number_match:
            index = int(number_match.group(1)) - 1
            if 0 <= index < len(candidates):
                return candidates[index]

        contour_aliases = {
            "green": ("green", "грин", "зелен", "зелё"),
            "blue": ("blue", "блю", "син"),
            "bh": ("bh", "бх"),
            "pl": ("pl", "пл"),
        }
        requested_contours = [
            contour
            for contour, aliases in contour_aliases.items()
            if any(alias in normalized for alias in aliases)
        ]
        if requested_contours:
            matched = []
            for candidate in candidates:
                candidate_text = self._normalize_command_text(" ".join([
                    str(candidate.get("category") or ""),
                    str(candidate.get("release_clean") or ""),
                    str(candidate.get("release_full") or ""),
                ]))
                for contour in requested_contours:
                    if any(alias in candidate_text for alias in contour_aliases[contour]):
                        matched.append(candidate)
                        break
            if len(matched) == 1:
                return matched[0]

        return None

    def _build_template_choice_response(self, flow: Dict, candidates: List[Dict]) -> Dict:
        release_key = flow.get("release_key", "")
        rov_key = flow.get("rov_key", "без РОВ")
        lines = [
            f"По *{release_key} / {rov_key}* нашел несколько вариантов шаблона.",
            "Выбери контур/шаблон:",
            "",
        ]
        for index, candidate in enumerate(candidates, 1):
            lines.append(f"{index}. `{self._format_template_candidate_label(candidate)}`")

        suggestions = [
            f"{index}. {self._format_template_candidate_label(candidate)}"
            for index, candidate in enumerate(candidates[:5], 1)
        ]
        suggestions.append("Отмена")

        return {
            "text": "\n".join(lines),
            "intent": "release_document_flow",
            "suggestions": suggestions,
            "metadata": {
                "type": "release_document_flow",
                "state": "template_choice_requested",
                "release_key": release_key,
                "row_key": flow.get("row_key", ""),
                "rov_key": rov_key,
                "candidates": candidates,
            },
        }

    def _apply_template_candidate_to_flow(self, flow: Dict, candidate: Dict, release_uses_playbooks_func) -> None:
        release_full = str(candidate.get("release_full") or "").strip()
        flow["category"] = str(candidate.get("category") or "").strip()
        flow["release_full"] = release_full
        flow["release_clean"] = str(candidate.get("release_clean") or "").strip()
        flow["playbooks_required"] = release_uses_playbooks_func(release_full)
        flow["state"] = "instruction_requested"

    def _build_release_doc_instruction_response(self, flow: Dict) -> Dict:
        release_key = flow.get("release_key", "")
        rov_key = flow.get("rov_key", "без РОВ")
        date_value = flow.get("date") or "-"
        oplot = flow.get("oplot") or "-"
        checker = flow.get("checker") or "-"
        template_label = flow.get("release_full") or self._format_template_candidate_label(flow)
        return {
            "text": (
                f"Нашел *{release_key} / {rov_key}* на {date_value}.\n"
                f"Шаблон: `{template_label}`.\n"
                f"OPLOT: `{oplot}`, проверяет: `{checker}`.\n\n"
                "Пришли ссылку на инструкцию Confluence или напиши `инструкции нет`."
            ),
            "intent": "release_document_flow",
            "suggestions": ["Инструкции нет", "Отмена"],
            "metadata": {
                "type": "release_document_flow",
                "state": "instruction_requested",
                "release_key": release_key,
                "row_key": flow.get("row_key", ""),
                "rov_key": rov_key,
            },
        }

    def _prepare_release_document_flow_for_item(
        self,
        item: Dict,
        session: ChatContext,
        *,
        original_message: str = "",
        batch_context: Optional[Dict] = None,
    ) -> Dict:
        release_key = str(item.get("release_key") or "").strip()
        row_key = str(item.get("row_key") or item.get("release_key") or release_key).strip()
        rov_key = str(item.get("rov_key") or "").strip() or "без РОВ"
        date_value = self._release_doc_date(item)
        oplot = str(item.get("psi_owner") or "").strip()
        checker = str(item.get("psi_checker") or "").strip()

        if not oplot:
            return {
                "text": f"По *{release_key} / {rov_key}* не назначен дежурный OPLOT. Сначала назначь ответственного в блоке релизов.",
                "intent": "release_document_flow",
                "suggestions": ["Открыть блок релизов", "Показать релизы недели по ответственному"],
                "metadata": {"type": "release_document_flow", "state": "missing_oplot", "release_key": release_key},
            }
        if not checker:
            session.active_release_flow = {
                "type": "release_document_flow",
                "state": "checker_requested",
                "release_key": release_key,
                "row_key": row_key,
                "rov_key": rov_key,
                "date": date_value,
                "batch_context": batch_context,
            }
            return {
                "text": f"По *{release_key} / {rov_key}* не заполнен проверяющий. Напиши ФИО проверяющего следующим сообщением, я сохраню его в таблицу и продолжу оформление документов.",
                "intent": "release_document_flow",
                "suggestions": ["Отмена"],
                "metadata": {"type": "release_document_flow", "state": "checker_requested", "release_key": release_key, "row_key": row_key, "rov_key": rov_key},
            }

        try:
            from routes.release_routes import (
                detect_release_template,
                get_release_version,
                get_ke_from_release,
                release_uses_playbooks,
                _get_previous_version_from_monitor_snapshot,
            )
            detection = detect_release_template(release_key)
            if detection.get("error"):
                raise ValueError(detection["error"])
            candidates = detection.get("candidates") or []
            jira_version = str(get_release_version(release_key) or "").strip()
            jira_ke = str(get_ke_from_release(release_key) or "").strip()
            missing_distribution_fields = []
            if not jira_version:
                missing_distribution_fields.append("release_version")
            if not jira_ke:
                missing_distribution_fields.append("ke")
            try:
                sync_release_monitor_jira_fields(
                    row_key=row_key,
                    release_key=release_key,
                    release_version=jira_version,
                    ke=jira_ke,
                )
            except Exception as exc:
                logging.warning("Release document flow Jira sync failed for %s: %s", release_key, exc)
            flow = {
                "type": "release_document_flow",
                "state": "instruction_requested",
                "release_key": release_key,
                "row_key": row_key,
                "rov_key": rov_key,
                "release_version": str(jira_version or item.get("release_version") or "").strip(),
                "prev_version": str(_get_previous_version_from_monitor_snapshot(row_key, release_key) or "").strip(),
                "oplot": oplot,
                "checker": checker,
                "date": date_value,
                "ke": str(jira_ke or item.get("ke") or "").strip(),
                "missing_distribution_fields": missing_distribution_fields,
                "playbooks": [],
                "instruction_link": "",
                "zni_key": str(item.get("zni_key") or item.get("base_zni_key") or "").strip(),
                "batch_context": batch_context,
            }

            if detection.get("found"):
                flow["category"] = detection.get("category", "")
                flow["release_full"] = detection.get("release_full", "")
                flow["release_clean"] = detection.get("release_clean", "")
                flow["playbooks_required"] = release_uses_playbooks(flow["release_full"])
            elif candidates:
                selected_candidate = self._select_template_candidate(candidates, original_message)
                if selected_candidate:
                    self._apply_template_candidate_to_flow(flow, selected_candidate, release_uses_playbooks)
                else:
                    flow["state"] = "template_choice_requested"
                    flow["template_candidates"] = candidates
                    session.active_release_flow = flow
                    return self._build_template_choice_response(flow, candidates)
            else:
                return {
                    "text": (
                        f"К сожалению, для *{release_key}* шаблоны документов сейчас не найдены. "
                        "Через ручной формирователь документов можно попробовать выбрать шаблон другого подходящего типа "
                        "и затем скорректировать сформированные файлы вручную."
                    ),
                    "intent": "release_document_flow",
                    "suggestions": ["Открыть ручной генератор", "Сформировать документы по релизу"],
                    "metadata": {"type": "release_document_flow", "state": "template_not_found", "release_key": release_key},
                }

            if missing_distribution_fields:
                flow["state"] = "distribution_requested"
                session.active_release_flow = flow
                missing_text = ", ".join(
                    "версия сборки" if field == "release_version" else "КЭ дистрибутива"
                    for field in missing_distribution_fields
                )
                return {
                    "text": (
                        f"В Jira по *{release_key}* не найден зарегистрированный дистрибутив: {missing_text}. "
                        "Рекомендуем сначала зарегистрировать дистрибутив в релизе. "
                        "Если документы нужно сформировать сейчас, пришли версию сборки и КЭ дистрибутива одним сообщением, "
                        "например: `D-01.001.00-201 CI15184160`."
                    ),
                    "intent": "release_document_flow",
                    "suggestions": ["Отмена"],
                    "metadata": {
                        "type": "release_document_flow",
                        "state": "distribution_requested",
                        "release_key": release_key,
                        "missing_distribution_fields": missing_distribution_fields,
                    },
                }

            session.active_release_flow = flow
        except Exception as exc:
            logging.error("Release document flow init failed: %s", exc)
            return {
                "text": f"Не удалось подготовить документы по *{release_key}*: {exc}",
                "intent": "release_document_flow",
                "suggestions": ["Открыть ручной генератор", "Сформировать документы по релизу"],
                "metadata": {"type": "release_document_flow", "state": "init_error", "release_key": release_key, "error": str(exc)},
            }

        return self._build_release_doc_instruction_response(session.active_release_flow)

    def _start_release_document_batch(self, session: ChatContext) -> Dict:
        flow = session.active_release_flow or {}
        source_items = flow.get("items") or []
        batch_items = []
        skipped_items = []
        seen = set()
        for source in source_items:
            row_key = str(source.get("row_key") or source.get("release_key") or "").strip()
            if not row_key or row_key in seen:
                continue
            item = self._get_release_row_by_row_key(row_key) or source
            if not item:
                continue
            seen.add(row_key)
            if self._is_release_document_batch_candidate(item):
                batch_items.append(item)
            else:
                skipped_items.append(item)

        if not batch_items:
            session.active_release_flow = None
            if skipped_items:
                skipped_text = ", ".join(
                    str(item.get("release_key") or item.get("row_key") or "").strip()
                    for item in skipped_items[:8]
                    if str(item.get("release_key") or item.get("row_key") or "").strip()
                )
                if len(skipped_items) > 8:
                    skipped_text += f" и еще {len(skipped_items) - 8}"
                return {
                    "text": (
                        "В последнем списке нет релизов, по которым сейчас нужно пакетно формировать документы: "
                        "все найденные строки уже в финальном/отмененном состоянии."
                        + (f"\n\nПропущены: {skipped_text}." if skipped_text else "")
                    ),
                    "intent": "release_document_batch",
                    "suggestions": ["Релизы недели по ответственному", "Сформировать документы по релизу", "Контроль недели"],
                    "metadata": {
                        "type": "release_document_batch",
                        "state": "empty_after_filter",
                        "skipped_count": len(skipped_items),
                    },
                }
            return {
                "text": "Не нашел релизы из предыдущего списка. Сначала запроси релизы недели по ответственному, а затем напиши `подготовить документы по всем`.",
                "intent": "release_document_batch",
                "suggestions": ["Релизы недели по ответственному", "Сформировать документы по релизу"],
                "metadata": {"type": "release_document_batch", "state": "empty"},
            }

        batch_context = {
            "items": [
                {
                    "row_key": item.get("row_key") or item.get("release_key") or "",
                    "release_key": item.get("release_key") or "",
                    "rov_key": item.get("rov_key") or "",
                }
                for item in batch_items
            ],
            "index": 0,
            "total": len(batch_items),
            "completed": [],
        }
        first_item = batch_items[0]
        response = self._prepare_release_document_flow_for_item(first_item, session, batch_context=batch_context)
        skipped_note = ""
        if skipped_items:
            skipped_note = (
                f" Финальные/отмененные строки пропускаю: {len(skipped_items)}."
            )
        response["text"] = (
            f"Запускаю поочередное оформление документов по {len(batch_items)} актуальным релизам из последнего списка.{skipped_note}\n\n"
            f"{response['text']}"
        )
        response["intent"] = "release_document_batch"
        response.setdefault("metadata", {})["batch"] = {
            "index": 0,
            "total": len(batch_items),
            "skipped_count": len(skipped_items),
        }
        return response

    def _handle_release_document_query(self, message: str, session: ChatContext) -> Dict:
        return self._handle_release_document_query_v2(message, session)

    def _handle_release_document_flow_reply(self, message: str, session: ChatContext) -> Dict:
        return self._handle_release_document_flow_reply_v2(message, session)

    def _extract_release_document_distribution_values(self, message: str, flow: Dict) -> Tuple[str, str]:
        text = str(message or "").strip()
        version_match = re.search(r"[DP]-\d+(?:\.\d+){2}(?:[.-][A-Za-z0-9_]+)+", text, re.IGNORECASE)
        ke_match = re.search(r"\bCI\s*0*\d{5,}\b", text, re.IGNORECASE)
        if not ke_match:
            ke_match = re.search(r"\b\d{5,}\b", text)

        version = version_match.group(0).strip() if version_match else str(flow.get("release_version") or "").strip()
        ke = ke_match.group(0).strip().replace(" ", "") if ke_match else str(flow.get("ke") or "").strip()
        return version, ke

    def _handle_release_document_query_v2(self, message: str, session: ChatContext) -> Dict:
        release_key = self._extract_release_key(message)
        if not release_key:
            session.active_release_flow = {
                "type": "release_document_flow",
                "state": "need_release_key",
                "created_at": datetime.now().isoformat(),
            }
            return {
                "text": "Нужен номер релиза. Пришли его одним сообщением, например `EMRM-12345`, и я продолжу формирование документов.",
                "intent": "release_document_flow",
                "suggestions": ["Отмена"],
                "metadata": {"type": "release_document_flow", "state": "need_release_key"},
            }

        rows = self._find_release_rows_for_key(release_key)
        if not rows:
            return {
                "text": f"Релиз {release_key} в текущем кеше блока релизов не найден. Обнови блок релизов и повтори запрос.",
                "intent": "release_document_flow",
                "suggestions": ["Открыть блок релизов", "Сформировать документы по релизу"],
                "metadata": {"type": "release_document_flow", "state": "release_not_found", "release_key": release_key},
            }

        return self._prepare_release_document_flow_for_item(rows[0], session, original_message=message)

    def _handle_release_document_flow_reply_v2(self, message: str, session: ChatContext) -> Dict:
        flow = session.active_release_flow or {}
        normalized = self._normalize_command_text(message)
        if "отмена" in normalized or "сброс" in normalized:
            session.active_release_flow = None
            return {
                "text": "Ок, сбросил сценарий формирования документов.",
                "intent": "release_document_flow",
                "suggestions": ["Сформировать документы по релизу", "Показать релизы недели по ответственному"],
                "metadata": {"type": "release_document_flow", "state": "cancelled"},
            }

        state = flow.get("state")
        if state == "need_release_key":
            release_key = self._extract_release_key(message)
            if not release_key:
                return {
                    "text": "Нужен номер релиза. Пришли его одним сообщением, например `SMECLM-37005`, и я продолжу формирование документов.",
                    "intent": "release_document_flow",
                    "suggestions": ["Отмена"],
                    "metadata": {"type": "release_document_flow", "state": "need_release_key"},
                }
            return self._handle_release_document_query_v2(release_key, session)

        if state == "distribution_requested":
            release_key = str(flow.get("release_key") or "").strip()
            row_key = str(flow.get("row_key") or release_key).strip()
            version, ke = self._extract_release_document_distribution_values(message, flow)
            if not version or not ke:
                return {
                    "text": (
                        "Нужно заполнить версию сборки и КЭ дистрибутива. "
                        "Пришли их одним сообщением, например: `D-01.001.00-201 CI15184160`."
                    ),
                    "intent": "release_document_flow",
                    "suggestions": ["Отмена"],
                    "metadata": {"type": "release_document_flow", "state": "distribution_requested"},
                }

            try:
                set_release_monitor_manual_distribution_override(row_key, release_version=version, ke=ke)
            except Exception as exc:
                logging.error("Release document manual distribution save failed: %s", exc)
                return {
                    "text": f"Не удалось сохранить ручные данные дистрибутива: {exc}",
                    "intent": "release_document_flow",
                    "suggestions": ["Попробовать еще раз", "Отмена"],
                    "metadata": {"type": "release_document_flow", "state": "distribution_save_error", "error": str(exc)},
                }

            flow["release_version"] = version
            flow["ke"] = ke
            flow["missing_distribution_fields"] = []
            flow["state"] = "instruction_requested"
            session.active_release_flow = flow
            return self._build_release_doc_instruction_response(flow)

        if state == "checker_requested":
            checker_name = message.strip()
            if not checker_name:
                return {
                    "text": "Напиши ФИО проверяющего одним сообщением. Например: `Иванов И.И.`",
                    "intent": "release_document_flow",
                    "suggestions": ["Отмена"],
                    "metadata": {"type": "release_document_flow", "state": "checker_requested"},
                }

            release_key = str(flow.get("release_key") or "").strip()
            row_key = str(flow.get("row_key") or "").strip()
            item = self._get_release_row_by_row_key(row_key)
            if not item:
                rows = self._find_release_rows_for_key(release_key)
                item = rows[0] if rows else None
            if not item:
                session.active_release_flow = None
                return {
                    "text": f"Не нашел строку релиза *{release_key}* в текущем кеше. Обнови блок релизов и повтори оформление документов.",
                    "intent": "release_document_flow",
                    "suggestions": ["Открыть блок релизов", "Сформировать документы по релизу"],
                    "metadata": {"type": "release_document_flow", "state": "release_not_found", "release_key": release_key},
                }

            try:
                self._save_release_assignment_from_item(item, checker=checker_name)
                item = dict(item)
                item["psi_checker"] = checker_name
            except Exception as exc:
                logging.error("Release checker save from chat failed: %s", exc)
                return {
                    "text": f"Не смог сохранить проверяющего `{checker_name}` по *{release_key}*: {exc}",
                    "intent": "release_document_flow",
                    "suggestions": ["Попробовать еще раз", "Отмена"],
                    "metadata": {"type": "release_document_flow", "state": "checker_save_error", "error": str(exc)},
                }

            session.active_release_flow = None
            next_response = self._prepare_release_document_flow_for_item(
                item,
                session,
                batch_context=flow.get("batch_context"),
            )
            next_response["text"] = f"Сохранил проверяющего `{checker_name}` в таблицу.\n\n{next_response['text']}"
            next_response.setdefault("metadata", {})["checker_saved"] = checker_name
            return next_response

        if state == "template_choice_requested":
            candidates = flow.get("template_candidates") or []
            selected_candidate = self._select_template_candidate(candidates, message)
            if not selected_candidate:
                return self._build_template_choice_response(flow, candidates)

            try:
                from routes.release_routes import release_uses_playbooks
                self._apply_template_candidate_to_flow(flow, selected_candidate, release_uses_playbooks)
            except Exception as exc:
                logging.error("Release template candidate selection failed: %s", exc)
                return {
                    "text": f"Не удалось выбрать шаблон для *{flow.get('release_key', '')}*: {exc}",
                    "intent": "release_document_flow",
                    "suggestions": ["Отмена"],
                    "metadata": {"type": "release_document_flow", "state": "template_choice_error", "error": str(exc)},
                }
            return self._build_release_doc_instruction_response(flow)

        if state == "instruction_requested":
            instruction_link = ""
            if "http://" in message or "https://" in message:
                link_match = re.search(r"https?://\S+", message)
                instruction_link = link_match.group(0).strip() if link_match else ""
            instruction_absent = any(marker in normalized for marker in ("инструкции нет", "инструкция нет", "нет инструкции", "без инструкции"))
            if not instruction_link and not instruction_absent:
                return {
                    "text": "Жду ссылку на инструкцию Confluence или фразу `инструкции нет`.",
                    "intent": "release_document_flow",
                    "suggestions": ["Инструкции нет", "Отмена"],
                    "metadata": {"type": "release_document_flow", "state": state},
                }
            flow["instruction_link"] = instruction_link
            if flow.get("prev_version"):
                flow["state"] = "prev_version_confirm"
                return {
                    "text": f"Версию отката подтянул автоматически: `{flow['prev_version']}`. Используем ее?",
                    "intent": "release_document_flow",
                    "suggestions": ["Да, используем", "Укажу другую версию", "Отмена"],
                    "metadata": {"type": "release_document_flow", "state": "prev_version_confirm"},
                }
            flow["state"] = "prev_version_requested"
            return {
                "text": "Не смог автоматически определить версию отката. Напиши ее вручную.",
                "intent": "release_document_flow",
                "suggestions": ["Отмена"],
                "metadata": {"type": "release_document_flow", "state": "prev_version_requested"},
            }

        if state == "prev_version_confirm":
            if self._is_yes(normalized):
                return self._advance_release_doc_after_prev_version(session)
            if self._is_no(normalized):
                flow["state"] = "prev_version_requested"
                return {
                    "text": "Ок, напиши нужную версию отката.",
                    "intent": "release_document_flow",
                    "suggestions": ["Отмена"],
                    "metadata": {"type": "release_document_flow", "state": "prev_version_requested"},
                }
            flow["prev_version"] = message.strip()
            return self._advance_release_doc_after_prev_version(session)

        if state == "prev_version_requested":
            value = message.strip()
            if not value:
                return {
                    "text": "Версия отката не должна быть пустой. Напиши значение, например `D-01.00.00-285`.",
                    "intent": "release_document_flow",
                    "suggestions": ["Отмена"],
                    "metadata": {"type": "release_document_flow", "state": state},
                }
            flow["prev_version"] = value
            return self._advance_release_doc_after_prev_version(session)

        if state == "playbooks_requested":
            flow["playbooks"] = self._parse_playbooks(message)
            return self._ask_release_doc_zni(session)

        if state == "zni_confirm":
            if flow.get("zni_key"):
                return self._generate_release_documents_from_flow(session, create_zni=False)
            create_zni = self._is_yes(normalized)
            if not create_zni and not self._is_no(normalized):
                return {
                    "text": "Создаем задачу в OPLOT? Ответь `да` или `нет`.",
                    "intent": "release_document_flow",
                    "suggestions": ["Да, создавай", "Нет, без ЗНИ", "Отмена"],
                    "metadata": {"type": "release_document_flow", "state": state},
                }
            return self._generate_release_documents_from_flow(session, create_zni=create_zni)

        return {
            "text": "Я потерял шаг сценария. Давай начнем заново: напиши `Сформировать документы по <релиз>`.",
            "intent": "release_document_flow",
            "suggestions": ["Сформировать документы по релизу"],
            "metadata": {"type": "release_document_flow", "state": "unknown"},
        }

    def _advance_release_doc_after_prev_version(self, session: ChatContext) -> Dict:
        flow = session.active_release_flow or {}
        if flow.get("playbooks_required"):
            flow["state"] = "playbooks_requested"
            return {
                "text": "По этому шаблону нужны плейбуки. Пришли список плейбуков одним сообщением.",
                "intent": "release_document_flow",
                "suggestions": ["Плейбуки не нужны", "Отмена"],
                "metadata": {"type": "release_document_flow", "state": "playbooks_requested"},
            }
        return self._ask_release_doc_zni(session)

    def _ask_release_doc_zni(self, session: ChatContext) -> Dict:
        flow = session.active_release_flow or {}
        flow["state"] = "zni_confirm"
        if flow.get("zni_key"):
            return {
                "text": f"В строке уже есть ЗНИ `{flow['zni_key']}`. Используем ее и формируем документы?",
                "intent": "release_document_flow",
                "suggestions": ["Да, формируй документы", "Отмена"],
                "metadata": {"type": "release_document_flow", "state": "zni_confirm"},
            }
        return {
            "text": "ЗНИ в строке пока нет. Создаем задачу в OPLOT перед формированием документов?",
            "intent": "release_document_flow",
            "suggestions": ["Да, создавай", "Нет, без ЗНИ", "Отмена"],
            "metadata": {"type": "release_document_flow", "state": "zni_confirm"},
        }

    def _generate_release_documents_from_flow(self, session: ChatContext, create_zni: bool = False) -> Dict:
        flow = session.active_release_flow or {}
        release_key = flow.get("release_key", "")
        try:
            zni_text = ""
            if create_zni and not flow.get("zni_key"):
                from services.release_monitor_service import create_release_monitor_zni
                issue_result = create_release_monitor_zni(flow.get("row_key", ""))
                issue = issue_result.get("issue", {})
                flow["zni_key"] = issue.get("key", "")
                zni_text = f"\nЗНИ создана: `{flow['zni_key']}`." if flow.get("zni_key") else ""

            from routes.release_routes import _generate_release_zip_buffer
            zip_buffer = _generate_release_zip_buffer(
                category=flow.get("category", ""),
                release_full=flow.get("release_full", ""),
                release_id=release_key,
                release_version=flow.get("release_version", ""),
                prev_version=flow.get("prev_version", ""),
                oplot=flow.get("oplot", ""),
                checker=flow.get("checker", ""),
                instruction_link=flow.get("instruction_link", ""),
                date_str=flow.get("date", ""),
                ke=flow.get("ke", ""),
                selected_playbooks=flow.get("playbooks", []),
            )
            document_id = self._save_release_zip_for_chat(release_key, zip_buffer)
            download_url = f"/dashboard/api/chat/release-docs/download/{document_id}"
            batch_context = flow.get("batch_context")
            if batch_context:
                completed = list(batch_context.get("completed") or [])
                completed.append({
                    "release_key": release_key,
                    "download_url": download_url,
                })
                batch_context["completed"] = completed
                next_index = int(batch_context.get("index") or 0) + 1
                batch_context["index"] = next_index
                batch_items = batch_context.get("items") or []

                if next_index < len(batch_items):
                    next_source = batch_items[next_index]
                    next_item = self._get_release_row_by_row_key(next_source.get("row_key")) or next_source
                    session.active_release_flow = None
                    next_response = self._prepare_release_document_flow_for_item(
                        next_item,
                        session,
                        batch_context=batch_context,
                    )
                    next_response["text"] = (
                        f"Документы по *{release_key}* готовы.{zni_text}\n"
                        f"[Скачать ZIP]({download_url})\n\n"
                        f"Переходим к релизу {next_index + 1} из {len(batch_items)}.\n\n"
                        f"{next_response['text']}"
                    )
                    next_response["intent"] = "release_document_batch"
                    next_response.setdefault("metadata", {})["batch"] = {
                        "index": next_index,
                        "total": len(batch_items),
                        "completed": completed,
                    }
                    return next_response

                session.active_release_flow = None
                lines = [
                    f"Документы по *{release_key}* готовы.{zni_text}",
                    f"[Скачать ZIP]({download_url})",
                    "",
                    f"Пакетное оформление завершено: {len(completed)} из {len(batch_items)}.",
                    "",
                    "*Готовые архивы:*",
                ]
                for item in completed:
                    lines.append(f"• {item['release_key']}: [Скачать ZIP]({item['download_url']})")
                return {
                    "text": "\n".join(lines),
                    "intent": "release_document_batch",
                    "suggestions": ["Релизы недели по ответственному", "Выгрузить таблицу релизов в Confluence"],
                    "metadata": {"type": "release_document_batch", "state": "completed", "completed": completed},
                }

            session.active_release_flow = None
            return {
                "text": f"Документы по *{release_key}* готовы.{zni_text}\n[Скачать ZIP]({download_url})",
                "intent": "release_document_flow",
                "suggestions": ["Показать релизы недели по ответственному", "Выгрузить таблицу релизов в Confluence"],
                "metadata": {"type": "release_document_flow", "state": "generated", "download_url": download_url},
            }
        except Exception as exc:
            logging.error("Release document generation from chat failed: %s", exc)
            return {
                "text": f"Не удалось сформировать документы по *{release_key}*: {exc}",
                "intent": "release_document_flow",
                "suggestions": ["Открыть ручной генератор", "Сформировать документы по релизу"],
                "metadata": {"type": "release_document_flow", "state": "generation_error", "error": str(exc)},
            }

    def _handle_release_confluence_export_query(self) -> Dict:
        year = datetime.now().year
        try:
            result = sync_release_monitor_assignments_from_confluence(year)
            updated = result.get("updated_rows", 0) if isinstance(result, dict) else 0
            page_url = "https://confluence.delta.sbrf.ru/pages/viewpage.action?pageId=18369778404"
            return {
                "text": f"Готово, выгрузил таблицу релизов в Confluence за {year} год. Обновлено строк: {updated}. Страница: [Таблица в Confluence]({page_url}).",
                "intent": "release_confluence_export",
                "suggestions": ["Показать релизы недели по ответственному", "Сформировать документы по релизу"],
                "metadata": {"type": "release_confluence_export", "result": result},
            }
        except Exception as exc:
            logging.error("Release Confluence export from chat failed: %s", exc)
            return {
                "text": f"Не удалось выполнить выгрузку в Confluence: {exc}",
                "intent": "release_confluence_export",
                "suggestions": ["Открыть блок релизов", "Что ты умеешь?"],
                "metadata": {"type": "release_confluence_export", "error": str(exc)},
            }

    def _handle_unknown(self) -> Dict:
        """Ответ для неподдерживаемых запросов."""
        return {
            'text': (
                "Я не до конца понял, что нужно сделать. Уточни одним сообщением: это про релиз, документы, Confluence, задачу Jira или сводку смены?\n\n"
                "Можно так: `релизы недели за Ивановым`, `оформи документы по EMRM-12345`, `контроль недели`, `найди задачу OPLOT-12345`."
            ),
            'suggestions': self.get_default_suggestions(),
            'metadata': {'type': 'unsupported'}
        }
    
    def _extract_params(self, message: str, intent: IntentType) -> Dict:
        """Извлекает параметры из сообщения в зависимости от намерения"""
        params = {}
        
        if intent == IntentType.SEARCH_TASKS:
            params = self.intent_classifier.extract_search_params(message)
        elif intent == IntentType.SPECIFIC_TASK:
            params['task_key'] = self.intent_classifier.extract_task_key(message)
        elif intent == IntentType.GENERATE_REPORT:
            # Извлекаем параметры для отчёта (дни или квартал)
            report_params = self.intent_classifier.extract_report_params(message)
            params.update(report_params)
        
        return params
    
    def _handle_greeting(self) -> Dict:
        """Обрабатывает приветствие"""
        return {
            'text': self._get_welcome_text(),
            'metadata': {'type': 'greeting'}
        }

    def _handle_show_capabilities(self) -> Dict:
        """Показывает все возможности бота"""
        return {
            'text': (
                "*Что умеет AI-бот Oplot*\n\n"
                "1. Работать с релизами: показать релизы недели по ответственному, собрать контроль недели, подготовить релизную статистику и предложить ответственных.\n"
                "Примеры: `Какие релизы текущей недели закреплены за Ивановым?`, `Покажи контроль недели`, `Предложи ответственных по релизам недели`.\n\n"
                "2. Формировать релизные документы тем же механизмом, что кнопка в строке релиза.\n"
                "Пример: `Сформировать документы по EMRM-12345`.\n\n"
                "3. Находить инструкцию и Jenkins job для раскатки на ПСИ по КЭ релиза.\n"
                "Пример: `Дай инструкцию ПСИ по SMECLM-12345`.\n\n"
                "4. Выгружать релизную таблицу в Confluence.\n"
                "Пример: `Выгрузи таблицу релизов в Confluence`.\n\n"
                "5. Помогать по рабочему столу дежурного: искать задачи, готовить сводку дневной/вечерней смены и статистику по сотрудникам.\n"
                "Примеры: `Найди задачу OPLOT-12345`, `Сводка дневной смены`, `Статистика за неделю`.\n\n"
                "Если запрос неоднозначный, я уточню недостающую деталь."
            ),
            'suggestions': self.get_default_suggestions(),
            'metadata': {'type': 'capabilities'}
        }
    
    def _handle_search(self, params: Dict, dashboard_context: Dict = None) -> Dict:
        """Обрабатывает поиск задач с использованием интеллектуального сервиса"""
        try:
            # Получаем оригинальное сообщение из сессии
            original_message = params.get('_original_message', '')
            
            # Используем новый интеллектуальный поиск
            result = self.search_service.search(original_message)
            
            return {
                'text': result['text'],
                'metadata': {
                    'task_count': len(result['tasks']),
                    'tasks': result['tasks'][:20],
                    'query': result['query']
                }
            }
            
        except Exception as e:
            logging.error(f"Ошибка поиска: {e}")
            return {
                'text': f'❌ Ошибка при поиске: {str(e)}',
                'metadata': {'error': str(e)}
            }
    
    def _handle_analysis(self, dashboard_context: Dict = None) -> Dict:
        """Обрабатывает анализ ситуации"""
        try:
            # Получаем актуальные данные
            if dashboard_context:
                all_tasks = (
                    dashboard_context.get('sup_tasks', []) +
                    dashboard_context.get('logi_tasks', []) +
                    dashboard_context.get('vnedrenie_prom_tasks', []) +
                    dashboard_context.get('vnedrenie_psi_tasks', [])
                )
            else:
                # Получаем данные напрямую
                data = get_dashboard_data()
                all_tasks = (
                    data.get('sup_tasks', []) +
                    data.get('logi_tasks', []) +
                    data.get('vnedrenie_prom_tasks', []) +
                    data.get('vnedrenie_psi_tasks', [])
                )
            
            if not all_tasks:
                return {
                    'text': '📊 Активных задач не обнаружено. Всё спокойно!',
                    'metadata': {'critical_count': 0}
                }
            
            # Анализируем задачи
            critical_tasks = []
            stale_tasks = []
            sup_tasks = []
            logi_tasks = []
            psi_tasks = []
            
            for task in all_tasks:
                days = task.get('days_in_progress', 0)
                
                if task.get('has_sup_tag'):
                    sup_tasks.append(task)
                if task.get('has_logi_tag'):
                    logi_tasks.append(task)
                if task.get('is_psi_task'):
                    psi_tasks.append(task)
                
                # Критичные: старше 5 дней или высокий приоритет
                if days > 5 or task.get('priority') in ['Highest', 'High']:
                    critical_tasks.append(task)
                
                # Зависшие: старше 10 дней
                if days > 10:
                    stale_tasks.append(task)
            
            # Формируем ответ
            text = "📊 *Анализ текущей ситуации:*\n\n"
            
            if critical_tasks:
                text += f"🔴 *Критичные задачи: {len(critical_tasks)}*\n"
                for task in critical_tasks[:5]:
                    text += f"• [{task['key']}]({task.get('url', '')}) - {task['summary'][:50]}... ({task.get('days_in_progress', 0)} дн.)\n"
                if len(critical_tasks) > 5:
                    text += f"... и ещё {len(critical_tasks) - 5}\n"
                text += "\n"
            
            text += f"📋 *Общая статистика:*\n"
            text += f"• СУП задачи: {len(sup_tasks)}\n"
            text += f"• Логи задачи: {len(logi_tasks)}\n"
            text += f"• ПСИ задачи: {len(psi_tasks)}\n"
            text += f"• Всего активных: {len(all_tasks)}\n"
            
            if stale_tasks:
                text += f"\n⚠️ *Зависшие задачи (>10 дней): {len(stale_tasks)}*\n"
            
            # Используем ГигаЧат для рекомендаций
            if self.giga_helper.client and critical_tasks:
                try:
                    prompt = self._create_analysis_prompt(critical_tasks, all_tasks)
                    giga_response = self.giga_helper.client.chat(prompt)
                    recommendations = giga_response.choices[0].message.content
                    text += f"\n💡 *AI-рекомендации:*\n{recommendations[:500]}..."
                except Exception as e:
                    logging.warning(f"Не удалось получить рекомендации от ГигаЧат: {e}")
            
            return {
                'text': text,
                'metadata': {
                    'critical_count': len(critical_tasks),
                    'total_count': len(all_tasks),
                    'sup_count': len(sup_tasks),
                    'logi_count': len(logi_tasks),
                    'psi_count': len(psi_tasks),
                }
            }
            
        except Exception as e:
            logging.error(f"Ошибка анализа: {e}")
            return {
                'text': f'❌ Ошибка при анализе: {str(e)}',
                'metadata': {'error': str(e)}
            }
    
    def _handle_guidance(self, params: Dict, dashboard_context: Dict = None) -> Dict:
        """Обрабатывает запрос на помощь/руководство"""
        task_key = params.get('task_key')
        
        if task_key:
            # Ищем конкретную задачу
            task = self._find_task_by_key(task_key, dashboard_context)
            if task:
                return self._provide_task_guidance(task)
        
        # Общая помощь по теме
        topic = params.get('topic', '')
        return {
            'text': (
                f"📖 *Помощь по теме: {topic}*\n\n"
                "Для получения конкретной помощи:\n"
                "• Укажите номер задачи (например, OPLOT-12345)\n"
                "• Опишите тип проблемы (СУП, логи, ПСИ)\n\n"
                "Частые сценарии:\n"
                "• Обработка СУП запросов\n"
                "• Работа с логами\n"
                "• ПСИ раскатки"
            ),
            'metadata': {'type': 'general_guidance'}
        }
    
    def _handle_report(self, params: Dict, dashboard_context: Dict = None, original_message: str = '') -> Dict:
        """Обрабатывает генерацию отчёта"""
        try:
            message_lower = original_message.lower()

            is_assignee_report = any(phrase in message_lower for phrase in [
                'по сотрудникам', 'статистика', 'сататист', 'сформируй', 'сгенерируй',
                'закрытые задачи.*сотрудник', 'эффективность', 'производительность'
            ])

            is_shift_handover = any(phrase in message_lower for phrase in [
                'передача смены', 'смена', 'смены'
            ])

            if is_shift_handover:
                return self._handle_shift_handover(params, dashboard_context, original_message)

            # Проверяем тип отчёта
            if self._is_release_report_request(message_lower, dashboard_context):
                return self._handle_release_statistics(params, dashboard_context, original_message)

            if is_assignee_report:
                return self._handle_assignee_statistics(params)
            else:
                # По умолчанию статистика по сотрудникам
                return self._handle_assignee_statistics(params)

        except Exception as e:
            logging.error(f"Ошибка генерации отчёта: {e}")
            return {
                'text': f'❌ Ошибка при генерации отчёта: {str(e)}',
                'metadata': {'error': str(e)}
            }
    
    def _is_release_report_request(self, message_lower: str, dashboard_context: Dict = None) -> bool:
        """Определяет, относится ли запрос к аналитике по релизной таблице."""
        release_items = (dashboard_context or {}).get('release_monitor') or []
        if not release_items:
            snapshot = get_release_monitor_snapshot() or {}
            release_items = snapshot.get('items') or []
        if not release_items:
            return False

        release_markers = [
            'релиз', 'релизы', 'ров', 'перераскат', 'хотфикс',
            'установлен', 'установлено', 'отменено', 'пром',
            'smecsc', 'smeclm', 'emrm', 'aigas', 'helperai',
        ]
        if any(marker in message_lower for marker in release_markers):
            return True

        if (dashboard_context or {}).get('page_context') == 'release_monitor':
            dashboard_only_markers = ['сотрудник', 'задач', 'смен', 'дежурн', 'сводк']
            if not any(marker in message_lower for marker in dashboard_only_markers):
                return True

        return False

    def _handle_release_statistics(self, params: Dict, dashboard_context: Dict = None, original_message: str = '') -> Dict:
        """Строит сводку и HTML-отчет по релизной таблице."""
        try:
            release_items = list((dashboard_context or {}).get('release_monitor') or [])
            if not release_items:
                snapshot = get_release_monitor_snapshot() or {}
                release_items = list(snapshot.get('items') or [])

            if not release_items:
                return {
                    'text': '📊 В таблице релизов пока нет данных для построения отчета.',
                    'metadata': {'total_tasks': 0, 'report_type': 'release_monitor'}
                }

            report_service = get_release_report_service()
            report_data = report_service.generate_release_report(
                release_items,
                quarter=params.get('quarter'),
                year=params.get('year'),
                days=params.get('days'),
                original_message=original_message,
            )

            if not report_data['items']:
                return {
                    'text': f"📊 За период *{report_data['period']['label']}* подходящих релизов не найдено.",
                    'metadata': {
                        'total_tasks': 0,
                        'report_type': 'release_monitor',
                        'period': report_data['period'],
                        'filters': report_data.get('filters', {}),
                    }
                }

            html_content = report_service.generate_html_report(report_data)
            report_id = save_report_to_disk(html_content)
            download_url = f"/dashboard/api/chat/report/download/{report_id}"

            stats = report_data['statistics']
            period = report_data['period']
            filters = report_data.get('filters', {})
            filter_title = {
                'all': 'всех релизов периода',
                'installed': 'установленных релизов',
                'reroll': 'перераскаток',
                'hotfix': 'хотфиксов',
                'cancelled': 'отмененных релизов',
            }.get(filters.get('kind', 'all'), 'релизов')

            text = f"📊 *Отчет по релизам*\n*{period['label']}*\n\n"
            text += f"Фильтр: {filter_title}\n"
            if filters.get('system'):
                text += f"Система: {filters['system']}\n"

            text += "\n*Итоги:*\n"
            text += f"• Строк в отчете: {stats['total']}\n"
            text += f"• Установлен на ПРОМ: {stats['installed']}\n"
            text += f"• Перераскаток: {stats['rerolls']}\n"
            text += f"• Хотфиксов: {stats['hotfixes']}\n"
            text += f"• Отменено: {stats['cancelled']}\n"

            if stats.get('systems'):
                text += "\n*По системам:*\n"
                for name, count in list(stats['systems'].items())[:5]:
                    text += f"• {name}: {count}\n"

            text += "\n*Первые релизы в отчете:*\n"
            preview_items = report_data['items'][:5]
            for item in preview_items:
                release_key = item.get('release_key') or '—'
                rov_key = item.get('rov_key') or 'без РОВ'
                date_value = item.get('deployment_end') or item.get('deployment_start') or 'без даты'
                text += f"• [{release_key}]({item.get('release_url', '')}) / {rov_key} — {date_value}\n"

            if len(report_data['items']) > len(preview_items):
                text += f"• ... и еще {len(report_data['items']) - len(preview_items)} строк\n"

            text += f"\n📥 *Скачать HTML-отчет:*\n[Нажмите здесь для скачивания отчета]({download_url})"

            return {
                'text': text,
                'suggestions': ["Контроль недели", "Отчет релизов текущей недели", "Выгрузить таблицу релизов в Confluence"],
                'metadata': {
                    'total_tasks': stats['total'],
                    'period': period,
                    'report_generated': True,
                    'report_id': report_id,
                    'download_url': download_url,
                    'report_type': 'release_monitor',
                    'filters': filters,
                }
            }
        except Exception as e:
            logging.error(f"Ошибка генерации релизного отчета: {e}")
            return {
                'text': f'❌ Ошибка при генерации отчета по релизам: {str(e)}',
                'metadata': {'error': str(e), 'report_type': 'release_monitor'}
            }

    def _handle_shift_handover(self, params: Dict, dashboard_context: Dict = None, original_message: str = '') -> Dict:
        """Обрабатывает сводку для передачи смены (дневная/вечерняя)"""
        try:
            message_lower = original_message.lower()

            # Определяем тип смены
            is_evening_shift = any(phrase in message_lower for phrase in [
                'вечер', 'вечерняя', '21', 'после 21', 'ночной'
            ])

            if dashboard_context:
                data = dashboard_context
            else:
                data = get_dashboard_data()

            hidden_task_keys = self._get_hidden_dashboard_task_keys()
            all_open_tasks = self._collect_dashboard_tasks(data, hidden_task_keys)
            closed_tasks = []

            if is_evening_shift:
                query = self.search_service.parse_query(original_message or 'сводка')
                closed_query = query
                closed_query.status = 'closed'
                closed_query.task_types = ['суп', 'логи', 'бд', 'инфра', 'роль', 'пси', 'внедрение']
                closed_query.keywords = []
                closed_query.summary_keywords = []
                closed_query.description_keywords = []
                closed_query.label_filters = []
                evening_tasks = []
                now = datetime.now().astimezone()
                evening_window_start, evening_window_end = self._get_evening_shift_window(now)
                closed_query.date_from = evening_window_start.strftime('%Y-%m-%d')
                closed_query.date_to = evening_window_end.strftime('%Y-%m-%d')
                closed_tasks = self.search_service.execute_search(closed_query, date_field='resolutiondate')

                for task in closed_tasks:
                    task_key = str(task.get('key') or '').strip()
                    if task_key and task_key in hidden_task_keys:
                        continue
                    closed_at = task.get('resolutiondate') or task.get('updated', '')
                    if not closed_at:
                        continue
                    try:
                        closed_dt = datetime.fromisoformat(closed_at.replace('Z', '+00:00'))
                    except ValueError:
                        continue
                    closed_dt = closed_dt.astimezone()
                    if evening_window_start <= closed_dt <= evening_window_end:
                        evening_tasks.append((closed_dt, task))

                evening_tasks.sort(key=lambda item: item[0], reverse=True)

                text = "🌙 *Вечерняя передача смены*\n"
                text += f"Окно: {evening_window_start.strftime('%d.%m %H:%M')} - {evening_window_end.strftime('%d.%m %H:%M')}\n\n"

                if evening_tasks:
                    text += f"✅ Закрыто за вечернюю смену: {len(evening_tasks)}\n"
                    visible_evening = evening_tasks[:5]
                    hidden_evening = evening_tasks[5:]
                    for closed_dt, task in visible_evening:
                        text += f"• [{task['key']}]({task.get('url', '')}) - {task['summary'][:65]}{'...' if len(task['summary']) > 65 else ''}\n"
                        text += f"  👤 {task['assignee_name']} | ⏰ {closed_dt.strftime('%d.%m %H:%M')}\n"
                    if hidden_evening:
                        text += self._format_expandable_task_block(
                            [
                                {
                                    'key': task['key'],
                                    'url': task.get('url', ''),
                                    'summary': task.get('summary', ''),
                                    'meta': f"👤 {task['assignee_name']} | ⏰ {closed_dt.strftime('%d.%m %H:%M')}"
                                }
                                for closed_dt, task in hidden_evening
                            ],
                            f"Показать еще {len(hidden_evening)} закрытых задач"
                        )
                else:
                    text += "✅ Закрытых задач в окно вечерней смены не найдено."

            else:
                text = self._format_day_shift_handover(data, hidden_task_keys)

            return {
                'text': text,
                'metadata': {
                    'shift_type': 'evening' if is_evening_shift else 'day',
                    'total_tasks': (
                        len(closed_tasks) + len(all_open_tasks)
                        if is_evening_shift
                        else (
                            len(self._filter_hidden_dashboard_tasks(data.get('sup_tasks', []) or [], hidden_task_keys))
                            + len(self._filter_hidden_dashboard_tasks(data.get('logi_tasks', []) or [], hidden_task_keys))
                        )
                    )
                }
            }

        except Exception as e:
            logging.error(f"Ошибка генерации сводки смены: {e}")
            return {
                'text': f'❌ Ошибка при генерации сводки смены: {str(e)}',
                'metadata': {'error': str(e)}
            }

    def _handle_assignee_statistics(self, params: Dict) -> Dict:
        """Обрабатывает генерацию отчёта по сотрудникам с закрытыми задачами"""
        try:
            from services.report_service import get_report_service, save_report_to_disk

            # Получаем параметры периода
            days = params.get('days')
            quarter = params.get('quarter')
            year = params.get('year')

            # Если не указан период, используем текущий квартал по умолчанию
            if not quarter and not days and not year:
                now = datetime.now()
                quarter = (now.month - 1) // 3 + 1
                year = now.year

            # Генерируем отчёт
            report_service = get_report_service()

            if quarter:
                report_data = report_service.generate_assignee_report(quarter=quarter, year=year)
                period_desc = f"{quarter} квартал {year or 'текущего года'}"
            elif year:
                report_data = report_service.generate_assignee_report(year=year)
                period_desc = f"{year} год"
            else:
                if not days:
                    days = 30
                report_data = report_service.generate_assignee_report(days=days)
                period_desc = f"последние {days} дней"
            
            if report_data['total_tasks'] == 0:
                return {
                    'text': f'📊 За {period_desc} закрытых задач не найдено.',
                    'metadata': {'total_tasks': 0}
                }
            
            # Генерируем и сохраняем HTML отчёт
            html_content = report_service.generate_html_report(report_data)
            report_id = save_report_to_disk(html_content)
            
            # Формируем краткую сводку для чата
            stats = report_data['statistics']
            period = report_data['period']
            
            # Заголовок с указанием периода
            if quarter:
                header = f"📊 *Статистика по сотрудникам*\n*{quarter} квартал {year or datetime.now().year}*\n"
            elif year:
                header = f"📊 *Статистика по сотрудникам*\n*{year} год*\n"
            else:
                header = f"📊 *Статистика по сотрудникам*\n*За последние {days} дней*\n"
            
            text = header
            text += f"Период: {period['start']} — {period['end']}\n\n"
            
            text += f"*Общие показатели:*\n"
            text += f"• Всего закрыто задач: {report_data['total_tasks']}\n"
            text += f"• Сотрудников в отчёте: {stats['assignee_count']}\n"
            text += f"• Среднее на сотрудника: {stats['avg_per_assignee']}\n\n"
            
            # Топ-3 сотрудника
            sorted_assignees = sorted(
                stats['by_assignee'].items(),
                key=lambda x: x[1]['total'],
                reverse=True
            )[:3]
            
            text += "*Топ сотрудников:*\n"
            for i, (name, data) in enumerate(sorted_assignees, 1):
                text += f"{i}. {name}: {data['total']} задач\n"
            
            # Распределение по всем тегам (динамически)
            text += f"\n*Распределение по тегам:*\n"
            tag_totals = stats['tag_totals']
            
            # Сортируем теги по количеству (по убыванию)
            sorted_tags = sorted(tag_totals.items(), key=lambda x: x[1], reverse=True)
            
            for tag, count in sorted_tags[:10]:  # Показываем топ-10 тегов
                tag_display = tag.capitalize() if tag != '(без тега)' else tag
                text += f"• {tag_display}: {count}\n"
            
            if len(sorted_tags) > 10:
                text += f"• ... и ещё {len(sorted_tags) - 10} тегов\n"
            
            # Показываем количество уникальных тегов
            if stats.get('all_tags'):
                text += f"\n_Всего уникальных тегов: {len(stats['all_tags'])}_\n"
            
            download_url = f"/dashboard/api/chat/report/download/{report_id}"
            text += f"\n📥 *Скачать полный отчёт:*\n"
            text += f"[Нажмите здесь для скачивания HTML отчёта]({download_url})\n"
            text += f"_Отчёт включает диаграммы и детальную статистику_"
            
            return {
                'text': text,
                'metadata': {
                    'total_tasks': report_data['total_tasks'],
                    'assignee_count': stats['assignee_count'],
                    'period': period,
                    'report_generated': True,
                    'report_id': report_id,
                    'download_url': download_url
                }
            }
            
        except Exception as e:
            logging.error(f"Ошибка генерации статистики: {e}")
            return {
                'text': f'❌ Ошибка при генерации статистики: {str(e)}',
                'metadata': {'error': str(e)}
            }

    def _collect_dashboard_tasks(self, dashboard_context: Dict, hidden_task_keys: Optional[set] = None) -> List[Dict]:
        """Собирает уникальные открытые задачи из колонок дашборда."""
        hidden_task_keys = hidden_task_keys if hidden_task_keys is not None else self._get_hidden_dashboard_task_keys()
        tasks = (
            dashboard_context.get('sup_tasks', []) +
            dashboard_context.get('logi_tasks', []) +
            dashboard_context.get('vnedrenie_prom_tasks', []) +
            dashboard_context.get('vnedrenie_psi_tasks', [])
        )
        unique_tasks = []
        seen = set()
        for task in tasks:
            key = str(task.get('key') or '').strip()
            if not key or key in seen:
                continue
            if key in hidden_task_keys:
                continue
            seen.add(key)
            unique_tasks.append(task)
        return unique_tasks

    def _get_hidden_dashboard_task_keys(self) -> set:
        try:
            return {str(key or "").strip() for key in get_hidden_task_keys() if str(key or "").strip()}
        except Exception as exc:
            logging.warning("Не удалось прочитать корзину задач рабочего стола: %s", exc)
            return set()

    def _filter_hidden_dashboard_tasks(self, tasks: List[Dict], hidden_task_keys: Optional[set] = None) -> List[Dict]:
        hidden_task_keys = hidden_task_keys if hidden_task_keys is not None else self._get_hidden_dashboard_task_keys()
        if not hidden_task_keys:
            return list(tasks or [])
        return [
            task for task in (tasks or [])
            if str((task or {}).get('key') or '').strip() not in hidden_task_keys
        ]

    def _get_welcome_text(self) -> str:
        return (
            "*AI-бот Oplot*\n\n"
            "Помогаю с релизами, документами, Confluence и рабочим столом дежурного.\n\n"
            "Могу:\n"
            "• показать релизы недели по ответственному;\n"
            "• сформировать документы по релизу;\n"
            "• найти инструкцию и Jenkins job для раскатки на ПСИ;\n"
            "• выгрузить таблицу релизов в Confluence;\n"
            "• собрать контроль недели и предложить ответственных;\n"
            "• найти задачи и подготовить сводку дневной или вечерней смены."
        )

    def _format_day_shift_handover(self, dashboard_context: Dict, hidden_task_keys: Optional[set] = None) -> str:
        hidden_task_keys = hidden_task_keys if hidden_task_keys is not None else self._get_hidden_dashboard_task_keys()
        sup_tasks = self._filter_hidden_dashboard_tasks(dashboard_context.get('sup_tasks', []) or [], hidden_task_keys)
        logi_tasks = self._filter_hidden_dashboard_tasks(dashboard_context.get('logi_tasks', []) or [], hidden_task_keys)
        today_releases = self._get_today_release_monitor_items()
        now = datetime.now()

        text = "☀️ *Дневная сводка OPLOT*\n"
        text += f"Сформировано: {now.strftime('%d.%m.%Y %H:%M')}\n\n"
        text += "*Фокус дня*\n"
        text += f"• СУП: {len(sup_tasks)} открытых\n"
        text += f"• Логи и операции: {len(logi_tasks)} открытых\n"
        text += f"• Релизы на сегодня: {len(today_releases)}\n\n"
        text += self._format_day_shift_focus_notes(sup_tasks, logi_tasks, today_releases)

        text += self._format_day_shift_task_section("СУП", sup_tasks)
        text += self._format_day_shift_task_section("Логи и операции", logi_tasks)
        text += self._format_today_releases_section(today_releases)

        if not sup_tasks and not logi_tasks and not today_releases:
            text += "Открытых задач и релизов на сегодня не найдено. Можно передавать смену без дополнительных акцентов.\n"

        return text.strip()

    def _format_day_shift_focus_notes(
        self,
        sup_tasks: List[Dict],
        logi_tasks: List[Dict],
        today_releases: List[Dict],
    ) -> str:
        notes = []
        stale_tasks = [
            task for task in [*sup_tasks, *logi_tasks]
            if self._task_days_in_progress(task) >= 5
        ]
        releases_without_responsible = [
            item for item in today_releases
            if self._release_monitor_responsibles_text(item) == "не назначены"
        ]

        if stale_tasks:
            notes.append(f"• {len(stale_tasks)} задач(и) старше 5 дней — стоит проверить комментарии и следующий шаг.")
        if releases_without_responsible:
            notes.append(f"• {len(releases_without_responsible)} релиз(а) на сегодня без ответственного — лучше назначить до окна установки.")
        if today_releases:
            notes.append("• Релизы ниже взяты из Блока релизов по тому же правилу, что фильтр «На сегодня».")

        if not notes:
            notes.append("• Критичных акцентов по открытым задачам и релизам на сегодня не видно.")

        return "*Акценты передачи*\n" + "\n".join(notes) + "\n\n"

    def _task_days_in_progress(self, task: Dict) -> int:
        try:
            return int(float(task.get('days_in_progress') or 0))
        except (TypeError, ValueError):
            return 0

    def _format_day_shift_task_section(self, title: str, tasks: List[Dict]) -> str:
        if not tasks:
            return f"*{title}*\nНет открытых задач в этом блоке.\n\n"

        lines = [f"*{title}* ({len(tasks)})"]
        visible_tasks = tasks[:5]
        hidden_tasks = tasks[5:]
        for task in visible_tasks:
            summary_raw = str(task.get('summary') or '').strip()
            summary = summary_raw[:62] + ('...' if len(summary_raw) > 62 else '')
            task_key = str(task.get('key') or '-').strip()
            task_url = str(task.get('url') or '').strip()
            assignee = str(task.get('assignee_name') or 'не назначен').strip()
            days = self._task_days_in_progress(task)
            lines.append(f"• [{task_key}]({task_url}) - {summary}")
            lines.append(f"  👤 {assignee} | ⏳ {days} дн.")

        text = "\n".join(lines) + "\n"
        if hidden_tasks:
            text += self._format_expandable_task_block(
                [
                    {
                        'key': task.get('key') or '-',
                        'url': task.get('url', ''),
                        'summary': task.get('summary', ''),
                        'meta': f"👤 {task.get('assignee_name') or 'не назначен'} | ⏳ {self._task_days_in_progress(task)} дн."
                    }
                    for task in hidden_tasks
                ],
                f"Показать еще {len(hidden_tasks)} задач"
            )
        return text + "\n"

    def _get_today_release_monitor_items(self) -> List[Dict]:
        snapshot = get_release_monitor_snapshot() or {}
        items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
        today = datetime.now().date()

        today_items = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            start_dt = self._release_start_dt(item)
            if not start_dt or start_dt.date() != today:
                continue
            row_key = str(item.get("row_key") or item.get("release_key") or "").strip()
            if row_key and row_key in seen:
                continue
            if row_key:
                seen.add(row_key)
            today_items.append(item)

        today_items.sort(key=lambda item: self._release_start_dt(item) or datetime.max)
        return today_items

    def _format_today_releases_section(self, releases: List[Dict]) -> str:
        if not releases:
            return "*Релизы на сегодня*\nПо фильтру «На сегодня» релизов к установке не найдено.\n\n"

        lines = [f"*Релизы на сегодня* ({len(releases)})"]
        visible_releases = releases[:6]
        hidden_releases = releases[6:]

        for item in visible_releases:
            lines.extend(self._format_today_release_lines(item))

        text = "\n".join(lines) + "\n"
        if hidden_releases:
            text += self._format_expandable_release_block(
                hidden_releases,
                f"Показать еще {len(hidden_releases)} релизов на сегодня"
            )
        return text + "\n"

    def _format_today_release_lines(self, item: Dict) -> List[str]:
        release_link = self._format_release_link(item.get("release_key"), item.get("release_url"))
        rov_key = str(item.get("rov_key") or "").strip()
        rov_url = str(item.get("rov_url") or "").strip()
        rov_link = self._format_release_link(rov_key, rov_url) if rov_key else "без РОВ"

        summary = self._release_monitor_display_summary(item)
        window = self._release_monitor_window_text(item)
        status = str(item.get("release_status") or "статус не указан").strip()
        system_name = str(item.get("system_name") or "система не указана").strip()
        responsibles = self._release_monitor_responsibles_text(item)
        zni = str(item.get("zni_key") or item.get("manual_zni_key") or "").strip()
        zni_text = f" | ЗНИ: {zni}" if zni else ""

        return [
            f"• {release_link} / {rov_link} — {window}",
            f"  {summary}",
            f"  Статус: {status} | Система: {system_name} | Ответственные: {responsibles}{zni_text}",
        ]

    def _format_expandable_release_block(self, releases: List[Dict], title: str) -> str:
        lines = [f"[details={title}]"]
        for item in releases:
            lines.extend(self._format_today_release_lines(item))
        lines.append("[/details]")
        return "\n".join(lines) + "\n"

    def _release_monitor_display_summary(self, item: Dict) -> str:
        lines = [
            str(line or "").strip()
            for line in (item.get("release_name_lines") or [])
            if str(line or "").strip()
        ]
        if lines:
            return " / ".join(lines[:2])
        return str(item.get("release_summary") or "описание релиза не заполнено").strip()

    def _release_monitor_window_text(self, item: Dict) -> str:
        start_dt = self._release_start_dt(item)
        end_dt = (
            self._parse_release_date(item.get("deployment_end_iso"))
            or self._parse_release_date(item.get("deployment_end"))
            or self._parse_release_date(item.get("source_deployment_end_iso"))
            or self._parse_release_date(item.get("source_deployment_end"))
        )
        if start_dt and end_dt:
            return f"{start_dt.strftime('%d.%m %H:%M')} - {end_dt.strftime('%d.%m %H:%M')}"
        if start_dt:
            return start_dt.strftime("%d.%m.%Y %H:%M")
        return str(item.get("deployment_start") or "дата не указана").strip()

    def _release_monitor_responsibles_text(self, item: Dict) -> str:
        responsibles = item.get("psi_responsibles") or []
        if not isinstance(responsibles, list):
            responsibles = [responsibles] if responsibles else []
        clean = [str(value or "").strip() for value in responsibles if str(value or "").strip()]
        return ", ".join(clean) if clean else "не назначены"

    def _format_expandable_task_block(self, tasks: List[Dict], title: str) -> str:
        """Формирует сворачиваемый блок с дополнительными задачами."""
        lines = [f"[details={title}]"]
        for task in tasks:
            summary = task.get('summary', '')[:62] + ('...' if len(task.get('summary', '')) > 62 else '')
            lines.append(f"• [{task['key']}]({task.get('url', '')}) - {summary}")
            meta = task.get('meta')
            if meta:
                lines.append(f"  {meta}")
        lines.append("[/details]")
        return "\n".join(lines) + "\n"

    def _get_evening_shift_window(self, now: datetime) -> Tuple[datetime, datetime]:
        if now.hour >= 21:
            window_start = now.replace(hour=21, minute=0, second=0, microsecond=0)
            window_end = (window_start + timedelta(hours=4))
            return window_start, min(now, window_end)

        if now.hour < 1:
            window_end = now.replace(hour=1, minute=0, second=0, microsecond=0)
            window_start = (window_end - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
            return window_start, min(now, window_end)

        window_end = now.replace(hour=1, minute=0, second=0, microsecond=0)
        window_start = (window_end - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
        return window_start, window_end
    
    def _handle_specific_task(self, params: Dict, dashboard_context: Dict = None) -> Dict:
        """Обрабатывает запрос о конкретной задаче"""
        task_key = params.get('task_key')
        
        if not task_key:
            return {
                'text': '❌ Не удалось определить ключ задачи',
                'metadata': {}
            }
        
        task = self._find_task_by_key(task_key, dashboard_context)
        
        if not task:
            # Попробуем найти в Jira напрямую
            task = self._fetch_task_from_jira(task_key)
        
        if task:
            return self._format_task_info(task)
        
        return {
            'text': f'❌ Задача {task_key} не найдена',
            'metadata': {'task_key': task_key}
        }
    
    def _ask_gigachat(self, message: str, session: ChatContext, dashboard_context: Dict = None) -> Dict:
        """Отправляет запрос к ГигаЧат как fallback"""
        if not self.giga_helper.client:
            if self._looks_like_casual_chat(message) and not self._looks_like_work_request(message):
                return {
                    'text': (
                        "Я на связи. Могу спокойно обсудить вопрос, помочь сформулировать мысль или быстро вернуться к рабочим задачам.\n\n"
                        "В этой среде свободные ответы через GigaChat сейчас недоступны, поэтому по сторонним темам отвечаю ограниченно. "
                        "А по релизам, документам, Confluence, задачам и сменным сводкам могу работать сразу."
                    ),
                    'suggestions': self.get_default_suggestions(),
                    'metadata': {'type': 'casual_fallback', 'source': 'local'}
                }
            return self._handle_unknown()

        try:
            dashboard_summary = self._get_dashboard_summary(dashboard_context)
            prompt = f"""Ты дружелюбный и краткий AI-бот Oplot для команды OPLOT.

Ты помогаешь с рабочим столом дежурного, Блоком релизов, релизными документами и выгрузкой в Confluence.
Не выдумывай факты, номера задач, релизы, ответственных и ссылки. Если для действия не хватает параметра, задай один конкретный уточняющий вопрос.
Если вопрос относится к поддерживаемому действию, подскажи точную формулировку команды.
Если пользователь просто общается, отвечай как живой рабочий помощник: коротко, без канцелярита, и мягко предлагай вернуться к делу.

Поддерживаемые примеры:
- `Релизы недели за Ивановым`
- `Какие релизы текущей недели закреплены за Ивановым?`
- `Оформи документы по EMRM-12345`
- `Сформировать документы по EMRM-12345`
- `Выгрузи таблицу релизов в Confluence`
- `Покажи контроль недели`
- `Предложи ответственных по релизам недели`
- `Сводка дневной смены`
- `Найди задачу OPLOT-12345`

Если вопрос сторонний и разговорный, можешь ответить по существу коротко.

Контекст дашборда:
{dashboard_summary}

Запрос пользователя:
{message}

Отвечай кратко, естественно и по-русски."""
            response = self.giga_helper.client.chat(prompt)
            text = response.choices[0].message.content.strip()
            return {
                'text': text or self._handle_unknown()['text'],
                'metadata': {'source': 'gigachat', 'mode': 'free_chat'}
            }
        except Exception as e:
            logging.warning(f"Ошибка fallback-ответа через GigaChat: {e}")
            return self._handle_unknown()
    
    def _search_all_tasks(self, params: Dict) -> List[Dict]:
        """Выполняет расширенный поиск по всем задачам"""
        # Формируем JQL запрос
        domain, token = get_jira_domain_and_token("OPLOT-1")  # Используем Delta для OPLOT
        
        jql_parts = ['project = OPLOT']
        
        if params.get('text'):
            jql_parts.append(f'text ~ "{params["text"]}"')
        
        if params.get('assignee'):
            jql_parts.append(f'assignee = "{params["assignee"]}"')
        
        status = params.get('status', 'all')
        if status == 'open':
            jql_parts.append('status NOT IN (Done, Closed, Resolved)')
        elif status == 'closed':
            jql_parts.append('status IN (Done, Closed, Resolved)')
        
        if params.get('tags'):
            tags_conditions = []
            for tag in params['tags']:
                tags_conditions.append(f'labels = "{tag}"')
            if tags_conditions:
                jql_parts.append(f'({" OR ".join(tags_conditions)})')
        
        # Временной диапазон
        date_range = params.get('date_range')
        if date_range == 'today':
            jql_parts.append(f'created >= "{datetime.now().strftime("%Y-%m-%d")}"')
        elif date_range == 'yesterday':
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            jql_parts.append(f'created >= "{yesterday}"')
        elif date_range == 'week':
            week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
            jql_parts.append(f'created >= "{week_ago}"')
        elif date_range == 'month':
            month_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            jql_parts.append(f'created >= "{month_ago}"')
        
        jql = ' AND '.join(jql_parts) + ' ORDER BY updated DESC'
        
        # Выполняем запрос
        try:
            url = f"{domain}/rest/api/2/search"
            headers = {"Authorization": f"Bearer {token}"}
            params_jira = {
                'jql': jql,
                'maxResults': 50,
                'fields': 'key,summary,created,updated,status,assignee,reporter,labels,priority,issuetype'
            }
            
            response = requests.get(url, headers=headers, params=params_jira, verify=False, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            issues = data.get('issues', [])
            
            # Трансформируем в наш формат
            tasks = []
            for issue in issues:
                assignee = issue['fields'].get('assignee')
                tasks.append({
                    'key': issue['key'],
                    'summary': issue['fields'].get('summary', ''),
                    'status': issue['fields'].get('status', {}).get('name', ''),
                    'assignee_name': assignee.get('displayName', 'Не назначен') if assignee else 'Не назначен',
                    'created': issue['fields'].get('created', ''),
                    'updated': issue['fields'].get('updated', ''),
                    'priority': issue['fields'].get('priority', {}).get('name', ''),
                    'labels': issue['fields'].get('labels', []),
                    'url': f"{domain}/browse/{issue['key']}"
                })
            
            return tasks
            
        except Exception as e:
            logging.error(f"Ошибка поиска в Jira: {e}")
            return []
    
    def _find_task_by_key(self, task_key: str, dashboard_context: Dict = None) -> Optional[Dict]:
        """Ищет задачу по ключу в контексте дашборда"""
        if not dashboard_context:
            return None
        
        all_tasks = (
            dashboard_context.get('sup_tasks', []) +
            dashboard_context.get('logi_tasks', []) +
            dashboard_context.get('vnedrenie_prom_tasks', []) +
            dashboard_context.get('vnedrenie_psi_tasks', [])
        )
        
        for task in all_tasks:
            if task['key'].upper() == task_key.upper():
                return task
        
        return None
    
    def _fetch_task_from_jira(self, task_key: str) -> Optional[Dict]:
        """Получает задачу напрямую из Jira"""
        try:
            domain, token = get_jira_domain_and_token(task_key)
            url = f"{domain}/rest/api/2/issue/{task_key}"
            headers = {"Authorization": f"Bearer {token}"}
            
            response = requests.get(url, headers=headers, verify=False, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            fields = data.get('fields', {})
            assignee = fields.get('assignee')
            
            return {
                'key': data['key'],
                'summary': fields.get('summary', ''),
                'status': fields.get('status', {}).get('name', ''),
                'assignee_name': assignee.get('displayName', 'Не назначен') if assignee else 'Не назначен',
                'created': fields.get('created', ''),
                'updated': fields.get('updated', ''),
                'priority': fields.get('priority', {}).get('name', ''),
                'labels': fields.get('labels', []),
                'description': fields.get('description', '')[:500],
                'url': f"{domain}/browse/{data['key']}"
            }
            
        except Exception as e:
            logging.error(f"Ошибка получения задачи из Jira: {e}")
            return None
    
    def _format_task_info(self, task: Dict) -> Dict:
        """Форматирует информацию о задаче для ответа"""
        status_emoji = {
            'Done': '✅',
            'Closed': '✅',
            'Resolved': '✅',
            'In Progress': '🔄',
            'To Do': '📋',
        }.get(task.get('status'), '📋')
        
        text = f"📋 *{task['key']}*\n"
        text += f"{status_emoji} *{task.get('summary', '')}*\n\n"
        text += f"📊 Статус: {task.get('status')}\n"
        text += f"👤 Исполнитель: {task.get('assignee_name')}\n"
        text += f"⚡ Приоритет: {task.get('priority', 'Не указан')}\n"
        text += f"🏷️ Теги: {', '.join(task.get('labels', []))}\n"
        text += f"📅 Создана: {task.get('created', '')[:10]}\n"
        text += f"🔗 [Открыть в Jira]({task.get('url', '')})\n"
        
        if task.get('description'):
            text += f"\n📝 Описание:\n{task['description'][:300]}..."
        
        return {
            'text': text,
            'metadata': {'task': task}
        }
    
    def _provide_task_guidance(self, task: Dict) -> Dict:
        """Предоставляет руководство по задаче"""
        task_type = self._determine_task_type(task)
        
        guidance_map = {
            'суп': self._get_sup_guidance(),
            'логи': self._get_logi_guidance(),
            'пси': self._get_psi_guidance(),
            'внедрение': self._get_vnedrenie_guidance(),
            'бд': self._get_db_guidance(),
            'инфра': self._get_infra_guidance(),
        }
        
        guidance = guidance_map.get(task_type, self._get_generic_guidance())
        
        text = f"📖 *Руководство по задаче {task['key']}*\n\n"
        text += f"Тип: {task_type.upper()}\n"
        text += f"Тема: {task.get('summary', '')[:60]}...\n\n"
        text += guidance
        
        return {
            'text': text,
            'metadata': {'type': 'guidance', 'task_type': task_type}
        }
    
    def _determine_task_type(self, task: Dict) -> str:
        """Определяет тип задачи"""
        labels = [l.lower() for l in task.get('labels', [])]
        summary = task.get('summary', '').lower()
        
        if 'суп' in labels or 'суп' in summary:
            return 'суп'
        if any(l in labels or l in summary for l in ['логи', 'logs']):
            return 'логи'
        if 'пси' in labels or 'раскат' in summary:
            return 'пси'
        if 'внедрение' in labels:
            return 'внедрение'
        if 'бд' in labels or 'база данных' in summary:
            return 'бд'
        if any(w in summary for w in ['под', 'pod', 'рестарт', 'инфра']):
            return 'инфра'
        
        return 'generic'
    
    def _get_sup_guidance(self) -> str:
        return """*Стандартные действия для СУП:*

1️⃣ *Проверка задачи:*
   • Прочитать описание полностью
   • Проверить наличие всех данных
   • Уточнить критичность

2️⃣ *Обработка:*
   • Взять в работу (In Progress)
   • Связаться с инициатором при необходимости
   • Выполнить требуемые действия

3️⃣ *Закрытие:*
   • Добавить комментарий с результатом
   • Проверить решение
   • Перевести в Done

⚠️ *При критичности High/Highest:*
   • Немедленная реакция
   • Информирование команды"""
    
    def _get_logi_guidance(self) -> str:
        return """*Работа с логами:*

1️⃣ *Сбор информации:*
   • Определить систему/сервис
   • Уточнить временной диапазон
   • Получить доступ к логам

2️⃣ *Анализ:*
   • Поиск по ключевым словам
   • Проверка временных меток
   • Выявление ошибок

3️⃣ *Передача:*
   • Архивация логов
   • Передача запросившему
   • Документирование"""
    
    def _get_psi_guidance(self) -> str:
        return """*ПСИ раскатка:*

1️⃣ *Подготовка:*
   • Проверить дистрибутивы
   • Убедиться в наличии бэкапа
   • Проверить окно внедрения

2️⃣ *Раскатка:*
   • Следовать плану
   • Мониторинг процесса
   • Фиксация результатов

3️⃣ *Проверка:*
   • Смок-тесты
   • Проверка логов
   • Подтверждение успеха"""
    
    def _get_vnedrenie_guidance(self) -> str:
        return """*Внедрение в ПРОМ:*

1️⃣ *Проверки перед внедрением:*
   • Согласования получены
   • План внедрения готов
   • Команда оповещена

2️⃣ *Внедрение:*
   • Строго по плану
   • Мониторинг метрик
   • Готовность к откату

3️⃣ *После внедрения:*
   • Проверка работоспособности
   • Информирование стейкхолдеров
   • Документирование результатов"""
    
    def _get_db_guidance(self) -> str:
        return """*Запросы к БД:*

1️⃣ *Валидация:*
   • Проверить SQL запрос
   • Подтвердить права доступа
   • Уточнить целевую БД

2️⃣ *Безопасность:*
   • Не выполнять DROP/DELETE без подтверждения
   • Проверить WHERE условие
   • Иметь бэкап

3️⃣ *Выполнение:*
   • В тестовой среде сначала
   • Логирование действий
   • Передача результатов"""
    
    def _get_infra_guidance(self) -> str:
        return """*Инфраструктурные операции:*

1️⃣ *Рестарт пода:*
   • Проверить текущий статус
   • Уведомить команду
   • Выполнить rolling restart
   • Проверить восстановление

2️⃣ *Работы по инфраструктуре:*
   • Плановое окно обслуживания
   • Бэкап конфигурации
   • Пошаговое выполнение
   • Валидация после работ"""
    
    def _get_generic_guidance(self) -> str:
        return """*Общие рекомендации:*

1️⃣ *Первичная обработка:*
   • Прочитать описание задачи
   • Оценить срочность
   • Взять в работу

2️⃣ *В процессе:*
   • Документировать действия
   • При необходимости - эскалация
   • Держать статус актуальным

3️⃣ *Завершение:*
   • Проверить результат
   • Добавить комментарий
   • Закрыть задачу"""
    
    def _create_analysis_prompt(self, critical_tasks: List[Dict], all_tasks: List[Dict]) -> str:
        """Создаёт промпт для анализа ситуации в ГигаЧат"""
        tasks_info = json.dumps([{
            'key': t['key'],
            'summary': t['summary'][:100],
            'days': t.get('days_in_progress', 0),
            'priority': t.get('priority', '')
        } for t in critical_tasks[:10]], ensure_ascii=False)
        
        return f"""Ты аналитик для дежурного инженера.

Критичные задачи ({len(critical_tasks)}):
{tasks_info}

Дай краткие рекомендации (макс. 300 символов):
1. Какие задачи требуют немедленного внимания
2. В каком порядке их обрабатывать
3. На что обратить внимание

Ответ:"""
    
    def _create_report_prompt(self, tasks: List[Dict], assignee_stats: Dict) -> str:
        """Создаёт промпт для генерации отчёта"""
        stats_info = json.dumps({
            'total': len(tasks),
            'by_assignee': {k: len(v) for k, v in list(assignee_stats.items())[:5]}
        }, ensure_ascii=False)
        
        return f"""Ты составляешь сводку для передачи смены дежурного инженера.

Данные:
{stats_info}

Сформулируй краткий вывод (макс. 200 символов) о текущей ситуации и рекомендации для следующей смены.

Вывод:"""
    
    def _get_dashboard_summary(self, dashboard_context: Dict = None) -> str:
        """Получает краткую сводку дашборда для контекста"""
        if not dashboard_context:
            try:
                dashboard_context = get_dashboard_data()
            except:
                return "Данные дашборда недоступны"
        
        total = (
            len(dashboard_context.get('sup_tasks', [])) +
            len(dashboard_context.get('logi_tasks', [])) +
            len(dashboard_context.get('vnedrenie_prom_tasks', [])) +
            len(dashboard_context.get('vnedrenie_psi_tasks', []))
        )
        release_summary = dashboard_context.get('release_monitor_summary', {})
        
        return f"""- Всего активных задач: {total}
- СУП задачи: {len(dashboard_context.get('sup_tasks', []))}
- Логи задачи: {len(dashboard_context.get('logi_tasks', []))}
- Внедрение: {len(dashboard_context.get('vnedrenie_prom_tasks', [])) + len(dashboard_context.get('vnedrenie_psi_tasks', []))}
- Релизы вне финального статуса: {release_summary.get('total', 0)}
- Просроченные релизы: {release_summary.get('overdue', 0)}"""


# Singleton для использования в приложении
_chatbot = None


def get_chatbot() -> DashboardChatBot:
    """Возвращает singleton экземпляр чат-бота"""
    global _chatbot
    if _chatbot is None:
        _chatbot = DashboardChatBot()
    return _chatbot
