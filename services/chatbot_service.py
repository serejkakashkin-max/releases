"""
Сервис чат-бота для дашборда дежурного.
Обрабатывает запросы пользователя и формирует ответы с помощью ГигаЧат.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
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
            
            # Классифицируем намерение
            intent = self.intent_classifier.classify(message)
            
            # Извлекаем параметры в зависимости от намерения
            params = self._extract_params(message, intent)
            
            # Для поиска добавляем оригинальное сообщение для интеллектуального парсинга
            if intent == IntentType.SEARCH_TASKS:
                params['_original_message'] = message
            
            # Обрабатываем запрос
            if intent == IntentType.GREETING:
                response = self._handle_greeting()
            elif intent == IntentType.SEARCH_TASKS:
                response = self._handle_search(params, dashboard_context)
            elif intent == IntentType.ANALYZE_SITUATION:
                response = self._handle_analysis(dashboard_context)
            elif intent == IntentType.TASK_GUIDANCE:
                response = self._handle_guidance(params, dashboard_context)
            elif intent == IntentType.GENERATE_REPORT:
                response = self._handle_report(params, dashboard_context, message)
            elif intent == IntentType.SPECIFIC_TASK:
                response = self._handle_specific_task(params, dashboard_context)
            else:
                # Используем ГигаЧат для неизвестных намерений
                response = self._ask_gigachat(message, session, dashboard_context)
            
            # Сохраняем в историю
            session.add_message('user', message, intent.value if intent else None)
            session.add_message('assistant', response['text'], metadata=response.get('metadata', {}))
            
            # Формируем результат
            return {
                'text': response['text'],
                'intent': intent.value if intent else 'unknown',
                'suggestions': self.intent_classifier.get_suggestions(intent),
                'metadata': response.get('metadata', {})
            }
            
        except Exception as e:
            logging.error(f"Ошибка обработки сообщения: {e}")
            return {
                'text': 'Произошла ошибка при обработке запроса. Попробуйте переформулировать.',
                'intent': 'error',
                'suggestions': ['Помощь', 'Показать все задачи'],
                'metadata': {'error': str(e)}
            }
    
    def _extract_params(self, message: str, intent: IntentType) -> Dict:
        """Извлекает параметры из сообщения в зависимости от намерения"""
        params = {}
        
        if intent == IntentType.SEARCH_TASKS:
            params = self.intent_classifier.extract_search_params(message)
        elif intent == IntentType.SPECIFIC_TASK:
            params['task_key'] = self.intent_classifier.extract_task_key(message)
        elif intent == IntentType.TASK_GUIDANCE:
            params['task_key'] = self.intent_classifier.extract_task_key(message)
            params['topic'] = message
        elif intent == IntentType.GENERATE_REPORT:
            # Извлекаем параметры для отчёта (дни или квартал)
            report_params = self.intent_classifier.extract_report_params(message)
            params.update(report_params)
        
        return params
    
    def _handle_greeting(self) -> Dict:
        """Обрабатывает приветствие"""
        return {
            'text': (
                "👋 Привет! Я AI-ассистент дежурного.\n\n"
                "Чем могу помочь?\n"
                "• Показать текущие задачи\n"
                "• Найти конкретную задачу\n"
                "• Проанализировать ситуацию\n"
                "• Сделать сводку для передачи смены"
            ),
            'metadata': {'type': 'greeting'}
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
            # Проверяем, запрошена ли статистика по сотрудникам
            message_lower = original_message.lower()
            is_assignee_report = any(phrase in message_lower for phrase in [
                'по сотрудникам', 'статистика.*сотрудник', 'сформируй.*статистику',
                'закрытые задачи.*сотрудник', 'эффективность', 'производительность'
            ])
            
            if is_assignee_report:
                return self._handle_assignee_statistics(params)
            
            # Стандартный отчёт для передачи смены
            if dashboard_context:
                data = dashboard_context
            else:
                data = get_dashboard_data()
            
            # Собираем статистику
            all_tasks = (
                data.get('sup_tasks', []) +
                data.get('logi_tasks', []) +
                data.get('vnedrenie_prom_tasks', []) +
                data.get('vnedrenie_psi_tasks', [])
            )
            
            # Группируем по дежурным
            assignee_stats = {}
            for task in all_tasks:
                assignee = task.get('assignee_name', 'Не назначен')
                if assignee not in assignee_stats:
                    assignee_stats[assignee] = []
                assignee_stats[assignee].append(task)
            
            # Формируем отчёт
            text = f"📋 *Сводка для передачи смены*\n"
            text += f"_{datetime.now().strftime('%d.%m.%Y %H:%M')}_\n\n"
            
            text += f"*Общая статистика:*\n"
            text += f"• Всего активных задач: {len(all_tasks)}\n"
            text += f"• СУП задачи: {len(data.get('sup_tasks', []))}\n"
            text += f"• Логи задачи: {len(data.get('logi_tasks', []))}\n"
            text += f"• Внедрение ПРОМ: {len(data.get('vnedrenie_prom_tasks', []))}\n"
            text += f"• Внедрение ПСИ: {len(data.get('vnedrenie_psi_tasks', []))}\n\n"
            
            if assignee_stats:
                text += "*Задачи по дежурным:*\n"
                for assignee, tasks in sorted(assignee_stats.items()):
                    text += f"• {assignee}: {len(tasks)} задач\n"
            
            # Используем ГигаЧат для улучшения отчёта
            if self.giga_helper.client:
                try:
                    prompt = self._create_report_prompt(all_tasks, assignee_stats)
                    giga_response = self.giga_helper.client.chat(prompt)
                    ai_summary = giga_response.choices[0].message.content
                    text += f"\n*AI-анализ:*\n{ai_summary}"
                except Exception as e:
                    logging.warning(f"Не удалось получить AI-анализ: {e}")
            
            return {
                'text': text,
                'metadata': {
                    'total_tasks': len(all_tasks),
                    'assignee_count': len(assignee_stats)
                }
            }
            
        except Exception as e:
            logging.error(f"Ошибка генерации отчёта: {e}")
            return {
                'text': f'❌ Ошибка при генерации отчёта: {str(e)}',
                'metadata': {'error': str(e)}
            }
    
    def _handle_assignee_statistics(self, params: Dict) -> Dict:
        """Обрабатывает генерацию отчёта по сотрудникам с закрытыми задачами"""
        try:
            from services.report_service import get_report_service, save_report_to_disk
            
            # Получаем параметры периода
            days = params.get('days', 30)
            quarter = params.get('quarter')
            year = params.get('year')
            
            # Генерируем отчёт
            report_service = get_report_service()
            
            if quarter:
                report_data = report_service.generate_assignee_report(quarter=quarter, year=year)
                period_desc = f"{quarter} квартал {year or 'текущего года'}"
            else:
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
            return {
                'text': (
                    "🤔 Я не совсем понял ваш запрос.\n\n"
                    "Попробуйте:\n"
                    "• 'Покажи все задачи'\n"
                    "• 'Что срочного?'\n"
                    "• 'Сводка для передачи'\n"
                    "• Или укажите номер задачи (OPLOT-12345)"
                ),
                'metadata': {'fallback': True}
            }
        
        try:
            # Формируем контекст для ГигаЧата
            context_info = self._get_dashboard_summary(dashboard_context)
            
            prompt = f"""Ты AI-ассистент дежурного инженера. Отвечай кратко и по существу.

Контекст дашборда:
{context_info}

Запрос пользователя: {message}

Если запрос непонятен - предложи варианты:
- Показать текущие задачи
- Анализ ситуации  
- Поиск задачи
- Сводка для передачи смены

Ответ:"""
            
            response = self.giga_helper.client.chat(prompt)
            return {
                'text': response.choices[0].message.content,
                'metadata': {'source': 'gigachat'}
            }
            
        except Exception as e:
            logging.error(f"Ошибка ГигаЧат: {e}")
            return {
                'text': 'Извините, не удалось обработать запрос. Попробуйте другую формулировку.',
                'metadata': {'error': str(e)}
            }
    
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
        
        return f"""- Всего активных задач: {total}
- СУП задачи: {len(dashboard_context.get('sup_tasks', []))}
- Логи задачи: {len(dashboard_context.get('logi_tasks', []))}
- Внедрение: {len(dashboard_context.get('vnedrenie_prom_tasks', [])) + len(dashboard_context.get('vnedrenie_psi_tasks', []))}"""


# Singleton для использования в приложении
_chatbot = None


def get_chatbot() -> DashboardChatBot:
    """Возвращает singleton экземпляр чат-бота"""
    global _chatbot
    if _chatbot is None:
        _chatbot = DashboardChatBot()
    return _chatbot
