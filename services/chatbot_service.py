"""
Сервис чат-бота для дашборда дежурного.
Обрабатывает запросы пользователя и формирует ответы с помощью ГигаЧат.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

from services.intent_classifier import IntentClassifier, IntentType, get_intent_classifier
from services.gigachat_service import GIGA_HELPER
from services.dashboard_service import (
    get_dashboard_data, fetch_jira_tasks, process_tasks_data,
    DASHBOARD_ASSIGNEES, DASHBOARD_DAYS_BACK
)
from services.jira_service import get_jira_domain_and_token
from services.chatbot_search_service import get_search_service
import requests
from services.report_service import save_report_to_disk
from services.release_monitor_service import get_release_monitor_snapshot
from services.release_report_service import get_release_report_service


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

            # Сначала используем локальную классификацию, затем при наличии GigaChat
            # даем модели шанс нормализовать "человеческую" формулировку.
            local_intent = self.intent_classifier.classify(message)
            resolved_intent, normalized_message, ai_plan = self._resolve_intent_and_message(
                message,
                local_intent,
                dashboard_context
            )
            if self._is_release_report_request((normalized_message or message).lower(), dashboard_context):
                resolved_intent = IntentType.GENERATE_REPORT

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
            return {
                'text': response['text'],
                'intent': resolved_intent.value if resolved_intent else 'unknown',
                'suggestions': response.get('suggestions', self.intent_classifier.get_suggestions(resolved_intent)),
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

    def _plan_with_gigachat(
        self,
        message: str,
        dashboard_context: Dict = None,
        local_intent: IntentType = IntentType.UNKNOWN
    ) -> Optional[Dict]:
        """Просит GigaChat определить сценарий и при необходимости нормализовать запрос."""
        if not self.giga_helper.client:
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
        for index, suggestion in enumerate(deduped_suggestions[:3], 1):
            text += f"{index}. {suggestion}\n"
        text += "\nОтветьте `да`, номером варианта, или напишите уточнение."

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
        local_intent = self.intent_classifier.classify(message)
        resolved_intent, normalized_message, ai_plan = self._resolve_intent_and_message(
            message,
            local_intent,
            dashboard_context
        )
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
            'suggestions': self.intent_classifier.get_suggestions(resolved_intent),
            'metadata': response.get('metadata', {})
        }

    def _looks_like_work_request(self, message: str) -> bool:
        """Грубая эвристика для отличия рабочего запроса от свободного разговора."""
        message_lower = message.lower()
        work_markers = [
            'задач', 'jira', 'сотрудник', 'статист', 'сататист', 'сводк', 'смен',
            'отчет', 'отчёт', 'суп', 'логи', 'бд', 'инфра', 'роль', 'пси', 'внедрение',
            'покажи', 'найди', 'сгенер', 'сформир', 'передач'
        ]
        return any(marker in message_lower for marker in work_markers)

        dashboard_summary = self._get_dashboard_summary(dashboard_context)
        prompt = f"""Ты маршрутизатор запросов для чат-бота дежурного инженера.

Текущий локально распознанный intent: {local_intent.value}

Поддерживаемые действия:
- search_tasks: показать или найти задачи
- generate_report: статистика или сводка передачи смены
- specific_task: запрос по конкретному ключу Jira
- show_capabilities: показать возможности бота
- greeting: приветствие
- free_chat: свободный разговор на стороннюю тему
- unknown: ничего не понятно

Контекст дашборда:
{dashboard_summary}

Запрос пользователя:
{message}

Верни только JSON:
{{
  "action": "one_of_supported_actions",
  "normalized_message": "нормализованная формулировка для запуска локальной логики или пустая строка",
  "confidence": "high|medium|low"
}}

Правила:
1. Если пользователь спрашивает о задачах, статистике, сводке или конкретной Jira-задаче, выбирай соответствующее действие.
2. normalized_message должен быть коротким, естественным и сохранять смысл запроса, исправляя опечатки и разговорные формулировки.
3. Если пользователь явно ушел в сторонний диалог, выбирай free_chat.
4. Не придумывай новые действия.
5. Ответ только JSON."""
        try:
            response = self.giga_helper.client.chat(prompt)
            content = response.choices[0].message.content
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group())
            return {
                'action': data.get('action'),
                'normalized_message': data.get('normalized_message', '').strip(),
                'confidence': data.get('confidence', 'low')
            }
        except Exception as e:
            logging.warning(f"Не удалось получить AI-план запроса: {e}")
            return None

    def _handle_unknown(self) -> Dict:
        """Ответ для неподдерживаемых запросов."""
        return {
            'text': (
                "Я это еще не умею, функция в стадии разработки.\n\n"
                "Сейчас доступны:\n"
                "• показать задачи за период;\n"
                "• найти задачи по ключевым словам, тегам, заголовку или тексту;\n"
                "• сгенерировать статистику за период;\n"
                "• сделать сводку для дневной или вечерней передачи смены.\n\n"
                "Если период не указан, использую текущий квартал."
            ),
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
                "*Что умеет AI-помощник*\n\n"
                "1. Найти задачи по тексту запроса.\n"
                "Примеры: `найди задачи по логам`, `найди задачи в описании focus.bh.new_clm_list.users`, `найди задачи в заголовке oracle`.\n\n"
                "2. Найти нужные задачи.\n"
                "Примеры: `найди задачи с тегом логи`, `найди задачу с текстом \"oracle\"`, `найди задачи со словом \"БД\" в заголовке`.\n\n"
                "3. Сформировать статистику по сотрудникам в HTML.\n"
                "Примеры: `сгенерируй статистику`, `статистика за 2 недели`, `статистика за 1 квартал 2026`.\n\n"
                "4. Подготовить сводку для передачи смены.\n"
                "Примеры: `сводка для передачи дневной смены`, `сводка для передачи вечерней смены`.\n\n"
                "Если период не указан, используется текущий квартал."
            ),
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

            # Проверяем тип отчёта
            if self._is_release_report_request(message_lower, dashboard_context):
                return self._handle_release_statistics(params, dashboard_context, original_message)

            is_assignee_report = any(phrase in message_lower for phrase in [
                'по сотрудникам', 'статистика', 'сататист', 'сформируй', 'сгенерируй',
                'закрытые задачи.*сотрудник', 'эффективность', 'производительность'
            ])

            is_shift_handover = any(phrase in message_lower for phrase in [
                'передача смены', 'смена', 'смены'
            ])

            if is_assignee_report:
                return self._handle_assignee_statistics(params)
            elif is_shift_handover:
                return self._handle_shift_handover(params, dashboard_context, original_message)
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

            all_open_tasks = self._collect_dashboard_tasks(data)
            query = self.search_service.parse_query(original_message or 'сводка')
            closed_query = query
            closed_query.status = 'closed'
            closed_query.task_types = ['суп', 'логи', 'бд', 'инфра', 'роль', 'пси', 'внедрение']
            closed_query.keywords = []
            closed_query.summary_keywords = []
            closed_query.description_keywords = []
            closed_query.label_filters = []
            closed_tasks = self.search_service.execute_search(closed_query)

            if is_evening_shift:
                evening_tasks = []
                now = datetime.now().astimezone()
                evening_window_start, evening_window_end = self._get_evening_shift_window(now)
                closed_query.date_from = evening_window_start.strftime('%Y-%m-%d')
                closed_query.date_to = evening_window_end.strftime('%Y-%m-%d')
                closed_tasks = self.search_service.execute_search(closed_query, date_field='resolutiondate')

                for task in closed_tasks:
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
                text = self._format_day_shift_handover(data)

            return {
                'text': text,
                'metadata': {
                    'shift_type': 'evening' if is_evening_shift else 'day',
                    'total_tasks': len(closed_tasks) + len(all_open_tasks)
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
            if not quarter and not days:
                now = datetime.now()
                quarter = (now.month - 1) // 3 + 1
                year = now.year

            # Генерируем отчёт
            report_service = get_report_service()

            if quarter:
                report_data = report_service.generate_assignee_report(quarter=quarter, year=year)
                period_desc = f"{quarter} квартал {year or 'текущего года'}"
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

    def _collect_dashboard_tasks(self, dashboard_context: Dict) -> List[Dict]:
        """Собирает уникальные открытые задачи из колонок дашборда."""
        tasks = (
            dashboard_context.get('sup_tasks', []) +
            dashboard_context.get('logi_tasks', []) +
            dashboard_context.get('vnedrenie_prom_tasks', []) +
            dashboard_context.get('vnedrenie_psi_tasks', [])
        )
        unique_tasks = []
        seen = set()
        for task in tasks:
            key = task.get('key')
            if not key or key in seen:
                continue
            seen.add(key)
            unique_tasks.append(task)
        return unique_tasks

    def _get_welcome_text(self) -> str:
        return (
            "*AI-помощник дежурного*\n\n"
            "Помогаю быстро находить задачи, собирать статистику и готовить сводки для передачи смены.\n\n"
            "Могу:\n"
            "• найти задачи по ключевым словам, тегам, заголовку или описанию;\n"
            "• сформировать статистику по сотрудникам в HTML;\n"
            "• подготовить сводку для передачи дневной или вечерней смены.\n\n"
            "Если не указать период, по умолчанию беру текущий квартал."
        )

    def _format_day_shift_handover(self, dashboard_context: Dict) -> str:
        sections = [
            ('СУП', dashboard_context.get('sup_tasks', [])),
            ('Логи и операции', dashboard_context.get('logi_tasks', [])),
            ('Внедрение ПРОМ', dashboard_context.get('vnedrenie_prom_tasks', [])),
            ('Внедрение ПСИ', dashboard_context.get('vnedrenie_psi_tasks', [])),
        ]

        total_open = sum(len(tasks) for _, tasks in sections)
        text = "☀️ *Дневная передача смены*\n"
        text += f"Открытых задач по блокам дашборда: {total_open}\n\n"

        non_empty_sections = 0
        for title, tasks in sections:
            if not tasks:
                continue
            non_empty_sections += 1
            text += f"*{title}* ({len(tasks)})\n"
            visible_tasks = tasks[:5]
            hidden_tasks = tasks[5:]
            for task in visible_tasks:
                summary = task['summary'][:62] + ('...' if len(task['summary']) > 62 else '')
                text += f"• [{task['key']}]({task.get('url', '')}) - {summary}\n"
                text += f"  👤 {task['assignee_name']} | ⏳ {task.get('days_in_progress', 0)} дн.\n"
            if hidden_tasks:
                text += self._format_expandable_task_block(
                    [
                        {
                            'key': task['key'],
                            'url': task.get('url', ''),
                            'summary': task.get('summary', ''),
                            'meta': f"👤 {task['assignee_name']} | ⏳ {task.get('days_in_progress', 0)} дн."
                        }
                        for task in hidden_tasks
                    ],
                    f"Показать еще {len(hidden_tasks)} задач"
                )
            text += "\n"

        if non_empty_sections == 0:
            text += "Открытых задач в блоках дашборда нет."

        return text.strip()

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
            return self._handle_unknown()

        try:
            dashboard_summary = self._get_dashboard_summary(dashboard_context)
            prompt = f"""Ты дружелюбный и краткий AI-помощник дежурного инженера.

Если вопрос относится к рабочим задачам, статистике, поиску задач, передаче смены или Jira,
в ответе мягко направь пользователя к поддерживаемым сценариям бота.

Если вопрос сторонний и разговорный, можешь ответить по существу как обычный собеседник.

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
