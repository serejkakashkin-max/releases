"""
Сервис интеллектуального поиска задач с использованием ГигаЧат.
Понимает естественный язык для запросов типа:
- "покажи все задачи СУП за текущий день"
- "задачи за последние двое суток"
- "СУП задачи с 15 по 20 февраля"
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from services.gigachat_service import GIGA_HELPER
from services.dashboard_service import (
    TAG_SUP_VARIANTS, TAG_LOGI_VARIANTS, TAG_VNEDRENIE_VARIANTS,
    TAG_PSI_VARIANTS, SUP_PATTERN, LOGI_PATTERN, PSI_PATTERN,
    get_jira_domain_and_token
)
import requests


@dataclass
class SearchQuery:
    """Структурированный поисковый запрос"""
    task_types: List[str]  # суп, логи, пси, внедрение, бд, инфра, роль
    status: str  # all, open, closed
    date_from: Optional[str]  # YYYY-MM-DD
    date_to: Optional[str]  # YYYY-MM-DD
    assignee: Optional[str]
    keywords: List[str]
    summary_keywords: List[str]
    description_keywords: List[str]
    label_filters: List[str]
    original_query: str


class ChatbotSearchService:
    """Сервис интеллектуального поиска задач"""
    
    # Соответствие типов задач и их вариаций
    TASK_TYPE_MAPPINGS = {
        'суп': ['суп', 'sup', 'суппорт', 'support'],
        'логи': ['логи', 'logi', 'logs', 'логирование'],
        'пси': ['пси', 'psi', 'предпроизводственное', 'тестирование'],
        'внедрение': ['внедрение', 'прод', 'production', 'релиз'],
        'бд': ['бд', 'db', 'база данных', 'database'],
        'инфра': ['инфра', 'infra', 'инфраструктура', 'под', 'pod'],
        'роль': ['роль', 'role', 'права', 'доступ', 'роли'],
    }

    DEFAULT_TASK_TYPES = ['суп', 'логи', 'бд', 'инфра', 'роль', 'пси', 'внедрение']
    TASK_TYPE_PATTERNS = {
        'суп': re.compile(r'\bсуп\w*|\bsup\w*', re.IGNORECASE),
        'логи': re.compile(r'\bлог\w*|\blog\w*', re.IGNORECASE),
        'пси': re.compile(r'\bпси\b|\bpsi\b', re.IGNORECASE),
        'внедрение': re.compile(r'\bвнедрен\w*|\bпрод\w*|\bрелиз\w*', re.IGNORECASE),
        'бд': re.compile(r'\bбд\b|\bdb\b|баз\w*\s+данн\w*', re.IGNORECASE),
        'инфра': re.compile(r'\bинфр\w*|\bпод\w*|\binfra\b', re.IGNORECASE),
        'роль': re.compile(r'\bрол\w*|\bправ\w*|\bдоступ\w*|\brole\b', re.IGNORECASE),
    }
    
    # Паттерны для извлечения дат
    DATE_PATTERNS = {
        'today': [
            r'сегодня',
            r'текущий день',
            r'этот день',
            r'за день',
        ],
        'yesterday': [
            r'вчера',
            r'прошлый день',
        ],
        'last_2_days': [
            r'последние двое суток',
            r'двое суток',
            r'2 дня',
            r'два дня',
            r'за два дня',
        ],
        'last_3_days': [
            r'последние трое суток',
            r'трое суток',
            r'3 дня',
            r'три дня',
        ],
        'last_week': [
            r'последняя неделя',
            r'за неделю',
            r'неделю',
            r'7 дней',
        ],
        'last_month': [
            r'последний месяц',
            r'за месяц',
            r'месяц',
            r'30 дней',
        ],
    }
    
    def __init__(self):
        self.giga_helper = GIGA_HELPER
    
    def parse_query(self, user_message: str) -> SearchQuery:
        """
        Разбирает запрос пользователя с помощью ГигаЧат или локального парсера.
        
        Args:
            user_message: Сообщение пользователя
            
        Returns:
            SearchQuery: Структурированный запрос
        """
        # Сначала пробуем локальный парсинг
        local_result = self._parse_local(user_message)
        
        # Если ГигаЧат доступен, используем его для уточнения
        if self.giga_helper.client:
            try:
                enhanced_result = self._parse_with_gigachat(user_message, local_result)
                if enhanced_result:
                    return enhanced_result
            except Exception as e:
                logging.warning(f"ГигаЧат не смог обработать запрос: {e}")
        
        return local_result
    
    def _parse_local(self, message: str) -> SearchQuery:
        """Локальный парсинг запроса"""
        message_lower = message.lower()
        unscoped_content_terms = self._extract_unscoped_content_terms(message)
        message_for_type_detection = message_lower
        if unscoped_content_terms:
            for term in unscoped_content_terms:
                message_for_type_detection = message_for_type_detection.replace(term.lower(), ' ')
        
        # Определяем типы задач
        task_types = []
        for task_type, pattern in self.TASK_TYPE_PATTERNS.items():
            if pattern.search(message_for_type_detection):
                task_types.append(task_type)
        
        if not task_types:
            task_types = self.DEFAULT_TASK_TYPES.copy()
        
        # Определяем статус
        status = 'all'
        if any(word in message_lower for word in ['закрыт', 'выполнен', 'done', 'closed', 'завершен', 'закрытые']):
            status = 'closed'
        elif any(word in message_lower for word in ['открыт', 'активен', 'open', 'текущий', 'открытые', 'в работе']):
            status = 'open'
        
        # Определяем даты
        date_from, date_to = self._extract_dates(message_lower)
        
        if date_from is None:
            date_from, date_to = self._get_current_quarter_dates()
        
        # Извлекаем исполнителя
        assignee = self._extract_assignee(message)

        summary_keywords, description_keywords, scoped_keywords = self._extract_scoped_keywords(message)
        label_filters = self._extract_label_filters(message)
        keywords = self._extract_keywords(message)
        if summary_keywords or description_keywords:
            keywords = []
        elif unscoped_content_terms:
            keywords = unscoped_content_terms
        if scoped_keywords:
            keywords = scoped_keywords

        return SearchQuery(
            task_types=task_types,
            status=status,
            date_from=date_from,
            date_to=date_to,
            assignee=assignee,
            keywords=keywords,
            summary_keywords=summary_keywords,
            description_keywords=description_keywords,
            label_filters=label_filters,
            original_query=message
        )
    
    def _parse_with_gigachat(self, message: str, local_result: SearchQuery) -> Optional[SearchQuery]:
        """Использует ГигаЧат для уточнения запроса"""
        today = datetime.now()
        
        prompt = f"""Ты парсер поисковых запросов для системы задач Jira.

Запрос пользователя: "{message}"

Текущая дата: {today.strftime('%Y-%m-%d')}

Извлеки параметры поиска и верни результат строго в JSON формате:
{{
    "task_types": ["список типов задач: суп, логи, пси, внедрение, бд, инфра, роль"],
    "status": "all|open|closed",
    "date_from": "YYYY-MM-DD или null",
    "date_to": "YYYY-MM-DD или null",
    "assignee": "имя исполнителя или null",
    "time_description": "описание временного периода на русском"
}}

Правила:
1. "сегодня" = {today.strftime('%Y-%m-%d')}
2. "вчера" = {(today - timedelta(days=1)).strftime('%Y-%m-%d')}
3. "последние двое суток" = с {(today - timedelta(days=2)).strftime('%Y-%m-%d')} по {today.strftime('%Y-%m-%d')}
4. "текущий день" = {today.strftime('%Y-%m-%d')}
5. Если дата не указана - используй null
6. Типы задач: суп, логи, пси, внедрение, бд, инфра, роль

Ответ только JSON, без дополнительного текста."""

        try:
            response = self.giga_helper.client.chat(prompt)
            content = response.choices[0].message.content
            
            # Извлекаем JSON из ответа
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                
                return SearchQuery(
                    task_types=data.get('task_types', local_result.task_types),
                    status=data.get('status', local_result.status),
                    date_from=(data.get('date_from') if data.get('date_from') != 'null' else None) or local_result.date_from,
                    date_to=(data.get('date_to') if data.get('date_to') != 'null' else None) or local_result.date_to,
                    assignee=data.get('assignee') if data.get('assignee') != 'null' else None,
                    keywords=local_result.keywords,
                    summary_keywords=local_result.summary_keywords,
                    description_keywords=local_result.description_keywords,
                    label_filters=local_result.label_filters,
                    original_query=message
                )
        except Exception as e:
            logging.error(f"Ошибка парсинга через ГигаЧат: {e}")
        
        return None

    def _get_current_quarter_dates(self) -> Tuple[str, str]:
        """Возвращает даты начала и конца текущего квартала."""
        now = datetime.now()
        quarter_start_month = ((now.month - 1) // 3) * 3 + 1
        start_date = datetime(now.year, quarter_start_month, 1)
        if quarter_start_month == 10:
            end_date = datetime(now.year, 12, 31)
        else:
            end_date = datetime(now.year, quarter_start_month + 3, 1) - timedelta(days=1)
        return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')
    
    def _extract_dates(self, message: str) -> Tuple[Optional[str], Optional[str]]:
        """Извлекает даты из сообщения"""
        today = datetime.now()
        date_from = None
        date_to = today.strftime('%Y-%m-%d')
        
        # Проверяем паттерны
        for period_type, patterns in self.DATE_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, message, re.IGNORECASE):
                    if period_type == 'today':
                        date_from = today.strftime('%Y-%m-%d')
                        date_to = today.strftime('%Y-%m-%d')
                    elif period_type == 'yesterday':
                        yesterday = today - timedelta(days=1)
                        date_from = yesterday.strftime('%Y-%m-%d')
                        date_to = yesterday.strftime('%Y-%m-%d')
                    elif period_type == 'last_2_days':
                        date_from = (today - timedelta(days=2)).strftime('%Y-%m-%d')
                    elif period_type == 'last_3_days':
                        date_from = (today - timedelta(days=3)).strftime('%Y-%m-%d')
                    elif period_type == 'last_week':
                        date_from = (today - timedelta(days=7)).strftime('%Y-%m-%d')
                    elif period_type == 'last_month':
                        date_from = (today - timedelta(days=30)).strftime('%Y-%m-%d')
                    return date_from, date_to
        
        # Проверяем конкретные даты (формат ДД.ММ.ГГГГ или ДД.ММ)
        date_patterns = [
            r'(\d{1,2})\.(\d{1,2})\.(\d{4})',  # ДД.ММ.ГГГГ
            r'(\d{1,2})\.(\d{1,2})',  # ДД.ММ (текущий год)
        ]
        
        dates_found = []
        for pattern in date_patterns:
            matches = re.findall(pattern, message)
            for match in matches:
                if len(match) == 3:
                    day, month, year = match
                    try:
                        date_obj = datetime(int(year), int(month), int(day))
                        dates_found.append(date_obj)
                    except ValueError:
                        continue
                elif len(match) == 2:
                    day, month = match
                    try:
                        date_obj = datetime(today.year, int(month), int(day))
                        dates_found.append(date_obj)
                    except ValueError:
                        continue
        
        if dates_found:
            dates_found.sort()
            date_from = dates_found[0].strftime('%Y-%m-%d')
            if len(dates_found) > 1:
                date_to = dates_found[-1].strftime('%Y-%m-%d')
            else:
                date_to = date_from
        
        return date_from, date_to
    
    def _extract_assignee(self, message: str) -> Optional[str]:
        """Извлекает имя исполнителя из сообщения"""
        # Паттерны для ФИО
        patterns = [
            r'(?:у|от|для|задачи)\s+([А-Я][а-я]+(?:\s+[А-Я]\.?){1,2})',
            r'([А-Я][а-я]+)\s+(?:делает|работает|назначен)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                return match.group(1)
        
        return None
    
    def _extract_keywords(self, message: str) -> List[str]:
        """Извлекает ключевые слова"""
        # Убираем стоп-слова - временные слова, предлоги и типы задач
        stop_words = {
            'покажи', 'показать', 'найди', 'найти', 'все', 'задачи', 'задача', 'задачу',
            'с', 'по', 'за', 'на', 'в', 'какие', 'какой', 'касаемую', 'которые', 'который',
            'текущий', 'текущие', 'день', 'дня', 'дней', 'сутки', 'суток',
            'вчера', 'сегодня', 'завтра', 'последний', 'последние', 'последняя',
            'неделя', 'неделю', 'месяц', 'месяца', 'год', 'года', 'квартал', 'квартала',
            'двое', 'трое', 'четверо', 'открытые', 'закрытые', 'активные', 'выполненные',
            'текст', 'тексте', 'текстом', 'описание', 'описании', 'заголовок', 'заголовке',
            'содержит', 'содержат', 'содержащие',
            'сотрудникам', 'сотрудники', 'статистика', 'статистику', 'сататистика', 'сататистику',
            'словом', 'слово', 'значением',
            'sup', 'logi', 'psi', 'db', 'infra', 'role',
        }
        words = re.findall(r'[\w\-]+', message.lower())
        keywords = []
        for word in words:
            if word in stop_words or len(word) <= 2 or word.isdigit():
                continue
            if any(pattern.search(word) for pattern in self.TASK_TYPE_PATTERNS.values()):
                continue
            keywords.append(word)
        return keywords[:3]  # Максимум 3 ключевых слова

    def _extract_scoped_keywords(self, message: str) -> Tuple[List[str], List[str], List[str]]:
        """Извлекает ключевые слова для заголовка и текста."""
        message_lower = message.lower()
        summary_keywords: List[str] = []
        description_keywords: List[str] = []

        quoted = re.findall(r'"([^"]+)"|«([^»]+)»', message)
        quoted_values = [item[0] or item[1] for item in quoted if item[0] or item[1]]

        if any(word in message_lower for word in ['заголов', 'теме', 'summary']):
            summary_inline = re.search(r'(?:слово|текст|значение)\s+(.+?)\s+в\s+(?:заголовк\w*|теме)', message, re.IGNORECASE)
            summary_source = self._extract_tail_after_markers(
                message,
                [r'в заголовк\w*', r'в теме', r'по заголовк\w*', r'summary']
            )
            summary_keywords = quoted_values or self._extract_scope_terms((summary_inline.group(1) if summary_inline else '') or summary_source or message)
        if any(word in message_lower for word in ['текст', 'описани', 'description']):
            description_inline = re.search(r'(?:слово|текст|значение)\s+(.+?)\s+в\s+описан\w*', message, re.IGNORECASE)
            description_source = self._extract_tail_after_markers(
                message,
                [r'с текст\w*', r'в тексте', r'в описан\w*', r'по текст\w*', r'description']
            )
            description_keywords = quoted_values or self._extract_scope_terms((description_inline.group(1) if description_inline else '') or description_source or message)

        scoped_keywords = []
        if not summary_keywords and not description_keywords and quoted_values:
            scoped_keywords = quoted_values[:3]

        return summary_keywords[:3], description_keywords[:3], scoped_keywords[:3]

    def _extract_tail_after_markers(self, message: str, markers: List[str]) -> str:
        """Возвращает хвост запроса после служебного маркера."""
        for marker in markers:
            match = re.search(marker + r'\s*(?:[:-]\s*)?(.+)$', message, re.IGNORECASE)
            if match:
                return match.group(1)
        return ''

    def _extract_scope_terms(self, text: str) -> List[str]:
        """Очищает хвост scoped-запроса от служебных слов."""
        cleaned = re.sub(
            r'\b(которые|который|которая|содержат|содержит|содержащие|со словом|слово|текст|значение|нужный|нужное)\b',
            ' ',
            text,
            flags=re.IGNORECASE
        )
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -:,.')
        if not cleaned:
            return []
        if ' ' in cleaned or len(cleaned) <= 3:
            return [cleaned]
        return self._extract_keywords(cleaned)

    def _extract_unscoped_content_terms(self, message: str) -> List[str]:
        """Извлекает поисковую фразу, если пользователь просит искать слово/текст без указания области."""
        match = re.search(
            r'(?:содерж\w*\s+(?:слово|текст|значение)|со\s+словом)\s*(?:[:-]\s*)?(.+)$',
            message,
            re.IGNORECASE
        )
        if not match:
            return []
        tail = match.group(1).strip(' -:,.')
        if not tail:
            return []
        if ' ' in tail:
            return [tail]
        return [tail]

    def _extract_label_filters(self, message: str) -> List[str]:
        """Извлекает фильтры по тегам."""
        message_lower = message.lower()
        label_filters = []
        if any(word in message_lower for word in ['тег', 'ярлык', 'label']):
            for task_type, pattern in self.TASK_TYPE_PATTERNS.items():
                if pattern.search(message_lower):
                    label_filters.append(task_type)
        return list(dict.fromkeys(label_filters))
    
    def build_jql(self, query: SearchQuery) -> str:
        """Строит JQL запрос из структурированного запроса, используя логику dashboard_service"""
        jql_parts = ['project = OPLOT']
        
        # Типы задач - строим условия как в dashboard_service.py
        type_conditions = []
        
        for task_type in query.task_types:
            if task_type == 'суп':
                # СУП: labels ИЛИ summary (как в dashboard_service.py)
                sup_summary_conditions = ' OR '.join([f'summary ~ "{variant}"' for variant in TAG_SUP_VARIANTS])
                sup_value_conditions = ' OR '.join([
                    f'summary ~ "значение {variant}" OR summary ~ "значение {variant.lower()}" OR summary ~ "Значение {variant}"'
                    for variant in TAG_SUP_VARIANTS
                ])
                type_conditions.append(
                    f'(labels = "СУП" OR labels = "суп" OR labels = "Суп" OR {sup_summary_conditions} OR {sup_value_conditions})'
                )
            elif task_type == 'логи':
                # Логи: labels ИЛИ summary
                type_conditions.append(
                    f'(labels = "Логи" OR labels = "логи" OR labels = "ЛОГИ" OR summary ~ "Логи" OR summary ~ "логи")'
                )
            elif task_type == 'пси':
                # ПСИ: labels
                type_conditions.append('(labels = "ПСИ" OR labels = "пси" OR labels = "Пси")')
            elif task_type == 'внедрение':
                # Внедрение: labels
                type_conditions.append('(labels = "Внедрение" OR labels = "внедрение")')
            elif task_type == 'бд':
                # БД: labels ИЛИ summary
                type_conditions.append(
                    f'(labels = "БД" OR labels = "бд" OR summary ~ "БД" OR summary ~ "бд")'
                )
            elif task_type == 'инфра':
                type_conditions.append(
                    f'(summary ~ "ПОД" OR summary ~ "под" OR summary ~ "работы по" OR summary ~ "Работы по")'
                )
            elif task_type == 'роль':
                # Роль: labels ИЛИ summary
                type_conditions.append(
                    f'(labels = "Роль" OR labels = "роль" OR summary ~ "роль" OR summary ~ "Роль")'
                )
        
        if type_conditions:
            jql_parts.append(f'({" OR ".join(type_conditions)})')
        
        # Статус
        if query.status == 'open':
            jql_parts.append('status NOT IN (Done, Closed, Resolved)')
        elif query.status == 'closed':
            jql_parts.append('status IN (Done, Closed, Resolved)')
        
        # Даты - используем формат с временем для включения всего дня
        if query.date_from:
            jql_parts.append(f'created >= "{query.date_from} 00:00"')
        if query.date_to:
            jql_parts.append(f'created <= "{query.date_to} 23:59"')
        
        # Исполнитель
        if query.assignee:
            jql_parts.append(f'assignee ~ "{query.assignee}"')
        
        if query.label_filters:
            label_conditions = []
            for task_type in query.label_filters:
                if task_type == 'суп':
                    label_conditions.extend(['labels = "СУП"', 'labels = "суп"', 'labels = "Суп"'])
                elif task_type == 'логи':
                    label_conditions.extend(['labels = "Логи"', 'labels = "логи"', 'labels = "ЛОГИ"'])
                elif task_type == 'пси':
                    label_conditions.extend(['labels = "ПСИ"', 'labels = "пси"', 'labels = "Пси"'])
                elif task_type == 'внедрение':
                    label_conditions.extend(['labels = "Внедрение"', 'labels = "внедрение"'])
                elif task_type == 'бд':
                    label_conditions.extend(['labels = "БД"', 'labels = "бд"'])
                elif task_type == 'роль':
                    label_conditions.extend(['labels = "Роль"', 'labels = "роль"'])
            if label_conditions:
                jql_parts.append(f'({" OR ".join(label_conditions)})')

        if query.summary_keywords:
            summary_conditions = [f'summary ~ "{kw}"' for kw in query.summary_keywords]
            jql_parts.append(f'({" AND ".join(summary_conditions)})')

        if query.description_keywords:
            description_conditions = [f'description ~ "{kw}"' for kw in query.description_keywords]
            jql_parts.append(f'({" AND ".join(description_conditions)})')

        if query.keywords:
            keyword_conditions = [f'(summary ~ "{kw}" OR description ~ "{kw}" OR text ~ "{kw}")' for kw in query.keywords]
            jql_parts.append(f'({" AND ".join(keyword_conditions)})')

        return ' AND '.join(jql_parts) + ' ORDER BY created DESC'
    
    def execute_search(self, query: SearchQuery) -> List[Dict]:
        """Выполняет поиск задач по JQL"""
        jql = self.build_jql(query)
        logging.info(f"[CHATBOT SEARCH] JQL запрос: {jql}")
        logging.info(f"[CHATBOT SEARCH] Параметры: types={query.task_types}, status={query.status}, date_from={query.date_from}, date_to={query.date_to}")
        
        try:
            domain, token = get_jira_domain_and_token()  # Используем Delta Jira как в dashboard_service
            logging.info(f"[CHATBOT SEARCH] Jira domain: {domain}")
            url = f"{domain}/rest/api/2/search"
            headers = {"Authorization": f"Bearer {token}"}
            params = {
                'jql': jql,
                'maxResults': 200,
                'fields': 'key,summary,created,updated,status,assignee,reporter,labels,priority,issuetype,description,resolutiondate'
            }
            
            response = requests.get(url, headers=headers, params=params, verify=False, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            issues = data.get('issues', [])
            logging.info(f"[CHATBOT SEARCH] Найдено задач: {len(issues)}")
            
            # Трансформируем
            tasks = []
            seen_keys = set()
            for issue in issues:
                if issue['key'] in seen_keys:
                    continue
                seen_keys.add(issue['key'])

                assignee = issue['fields'].get('assignee')
                
                # Определяем типы задачи
                labels = [l.lower() for l in issue['fields'].get('labels', [])]
                has_sup = 'суп' in labels
                has_logi = 'логи' in labels
                has_psi = 'пси' in labels
                
                tasks.append({
                    'key': issue['key'],
                    'summary': issue['fields'].get('summary', ''),
                    'status': issue['fields'].get('status', {}).get('name', ''),
                    'assignee_name': assignee.get('displayName', 'Не назначен') if assignee else 'Не назначен',
                    'created': issue['fields'].get('created', ''),
                    'updated': issue['fields'].get('updated', ''),
                    'priority': issue['fields'].get('priority', {}).get('name', ''),
                    'labels': issue['fields'].get('labels', []),
                    'description': issue['fields'].get('description', '') or '',
                    'resolutiondate': issue['fields'].get('resolutiondate', ''),
                    'has_sup_tag': has_sup,
                    'has_logi_tag': has_logi,
                    'is_psi_task': has_psi,
                    'url': f"{domain}/browse/{issue['key']}"
                })
            
            return tasks
            
        except Exception as e:
            logging.error(f"Ошибка поиска в Jira: {e}")
            return []
    
    def format_results(self, tasks: List[Dict], query: SearchQuery) -> str:
        """Форматирует результаты поиска для ответа пользователю - показывает все задачи сразу без сворачивания"""
        if not tasks:
            period_desc = self._get_period_description(query)
            types_desc = ', '.join(query.task_types) if query.task_types else 'все'
            status_desc = 'закрытые' if query.status == 'closed' else ('открытые' if query.status == 'open' else 'все')
            
            return f'🔍 Задачи не найдены\n\nПараметры поиска:\n• Типы: {types_desc}\n• Статус: {status_desc}\n• Период: {period_desc}'
        
        # Формируем описание периода
        period_desc = self._get_period_description(query)
        
        # Формируем заголовок
        types_label = ', '.join(t.upper() for t in query.task_types) if query.task_types else 'Все'
        status_label = {
            'open': 'открытые',
            'closed': 'закрытые',
            'all': 'все'
        }.get(query.status, 'все')
        
        text = f"🔍 *Результаты поиска*\n"
        text += f"📋 {types_label} задачи ({status_label})\n"
        text += f"📅 {period_desc}\n"
        text += f"📊 Найдено: {len(tasks)}\n\n"

        # Группируем по статусу
        open_tasks = [t for t in tasks if t['status'] not in ['Done', 'Closed', 'Resolved']]
        closed_tasks = [t for t in tasks if t['status'] in ['Done', 'Closed', 'Resolved']]

        if open_tasks:
            text += f"🟡 *Открытые ({len(open_tasks)}):*\n"
            for task in open_tasks[:10]:
                labels = ', '.join(task.get('labels', [])[:3]) or 'без тегов'
                text += f"• [{task['key']}]({task['url']}) - {self._escape_markdown(task['summary'][:70])}{'...' if len(task['summary']) > 70 else ''}\n"
                text += f"  👤 {self._escape_markdown(task['assignee_name'][:30])} | 🏷️ {self._escape_markdown(labels)}\n"
            text += "\n"

        if closed_tasks:
            text += f"✅ *Закрытые ({len(closed_tasks)}):*\n"
            for task in closed_tasks[:10]:
                labels = ', '.join(task.get('labels', [])[:3]) or 'без тегов'
                text += f"• [{task['key']}]({task['url']}) - {self._escape_markdown(task['summary'][:70])}{'...' if len(task['summary']) > 70 else ''}\n"
                text += f"  👤 {self._escape_markdown(task['assignee_name'][:30])} | 🏷️ {self._escape_markdown(labels)}\n"

        if len(tasks) > 10:
            text += f"\n_Показаны первые {min(len(tasks), 20)} задач из {len(tasks)}._"

        return text
    
    def _escape_markdown(self, text: str) -> str:
        """Экранирует специальные символы markdown в тексте"""
        # Экранируем квадратные скобки, которые могут сломать markdown ссылки
        return text.replace('[', '\\[').replace(']', '\\]')
    
    def _get_period_description(self, query: SearchQuery) -> str:
        """Формирует описание периода для отображения"""
        if query.date_from and query.date_to:
            if query.date_from == query.date_to:
                # Конкретная дата
                try:
                    date_obj = datetime.strptime(query.date_from, '%Y-%m-%d')
                    return f"за {date_obj.strftime('%d.%m.%Y')}"
                except:
                    return f"с {query.date_from}"
            else:
                return f"с {query.date_from} по {query.date_to}"
        elif query.date_from:
            return f"с {query.date_from}"
        else:
            return "за всё время"
    
    def search(self, user_message: str) -> Dict:
        """
        Главный метод поиска.
        
        Args:
            user_message: Сообщение пользователя
            
        Returns:
            Dict: Результат с текстом ответа и метаданными
        """
        # Парсим запрос
        query = self.parse_query(user_message)
        
        # Выполняем поиск
        tasks = self.execute_search(query)
        
        # Форматируем ответ
        response_text = self.format_results(tasks, query)
        
        return {
            'text': response_text,
            'tasks': tasks,
            'query': {
                'task_types': query.task_types,
                'status': query.status,
                'date_from': query.date_from,
                'date_to': query.date_to,
                'jql': self.build_jql(query)
            }
        }


# Singleton
_search_service = None


def get_search_service() -> ChatbotSearchService:
    """Возвращает singleton экземпляр сервиса поиска"""
    global _search_service
    if _search_service is None:
        _search_service = ChatbotSearchService()
    return _search_service
