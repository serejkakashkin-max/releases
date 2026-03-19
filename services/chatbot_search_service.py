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
from config import DASHBOARD_ASSIGNEES
from services.dashboard_service import (
    TAG_SUP_VARIANTS, TAG_LOGI_VARIANTS, TAG_VNEDRENIE_VARIANTS,
    TAG_PSI_VARIANTS, TAG_ROLE_VARIANTS, DB_PATTERNS, INFRA_PATTERNS, ROLE_PATTERNS,
    SUP_PATTERN, LOGI_PATTERN, PSI_PATTERN,
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
    ai_confidence: str = 'local'
    assignee_scope: str = 'duty'
    explicit_task_types: bool = False


class ChatbotSearchService:
    """Сервис интеллектуального поиска задач"""

    LABEL_VARIANTS = {
        'суп': list(dict.fromkeys(TAG_SUP_VARIANTS + ['SUP', 'sup', 'Sup'])),
        'логи': list(dict.fromkeys(TAG_LOGI_VARIANTS + ['LOGI', 'logi', 'Logi', 'LOGS', 'logs', 'Logs'])),
        'пси': list(dict.fromkeys(TAG_PSI_VARIANTS + ['PSI', 'psi', 'Psi'])),
        'внедрение': list(dict.fromkeys(TAG_VNEDRENIE_VARIANTS)),
        'бд': ['БД', 'бд', 'Бд', 'DB', 'db', 'Db'],
        'инфра': ['Инфра', 'инфра', 'ИНФРА', 'INFRA', 'infra', 'Infra'],
        'роль': list(dict.fromkeys(TAG_ROLE_VARIANTS + ['ROLE', 'role', 'Role'])),
    }
    
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

        label_filters = self._extract_label_filters(message)
        if not task_types and label_filters:
            task_types = label_filters.copy()
        explicit_task_types = bool(task_types)
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
        assignee_scope = self._extract_assignee_scope(message)

        summary_keywords, description_keywords, scoped_keywords = self._extract_scoped_keywords(message)
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
            original_query=message,
            assignee_scope=assignee_scope,
            explicit_task_types=explicit_task_types
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
    "scope": "anywhere|summary|description|labels",
    "search_text": "основной текст поиска или null",
    "assignee_scope": "duty|all_oplot",
    "confidence": "low|medium|high"
}}

Правила:
1. "сегодня" = {today.strftime('%Y-%m-%d')}
2. "вчера" = {(today - timedelta(days=1)).strftime('%Y-%m-%d')}
3. "последние двое суток" = с {(today - timedelta(days=2)).strftime('%Y-%m-%d')} по {today.strftime('%Y-%m-%d')}
4. "текущий день" = {today.strftime('%Y-%m-%d')}
5. Если дата не указана - используй null
6. Типы задач: суп, логи, пси, внедрение, бд, инфра, роль
7. Если пользователь просит искать "в описании" - scope=description.
8. Если пользователь просит искать "в заголовке" или "в теме" - scope=summary.
9. Если пользователь пишет "по ключевому слову", "содержит", "присутствует", но не указывает область - scope=anywhere.
10. search_text должен содержать только полезную строку поиска без служебных слов.
11. Если в запросе есть техническая строка вида focus.bh.new_clm_list.users, верни ее целиком в search_text.
12. Если пользователь просит искать по всем людям, всем пользователям, всем сотрудникам, по всей группе OPLOT, по всем из OPLOT - assignee_scope=all_oplot.
13. Если это не указано явно - assignee_scope=duty.

Ответ только JSON, без дополнительного текста."""

        try:
            response = self.giga_helper.client.chat(prompt)
            content = response.choices[0].message.content
            
            # Извлекаем JSON из ответа
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                ai_query = self._build_query_from_ai_data(message, local_result, data)
                if ai_query:
                    return ai_query

        except Exception as e:
            logging.error(f"Ошибка парсинга через ГигаЧат: {e}")
        
        return None

    def _build_query_from_ai_data(self, message: str, local_result: SearchQuery, data: Dict) -> Optional[SearchQuery]:
        task_types = self._normalize_task_types(data.get('task_types')) or local_result.task_types
        status = data.get('status', local_result.status)
        date_from = (data.get('date_from') if data.get('date_from') != 'null' else None) or local_result.date_from
        date_to = (data.get('date_to') if data.get('date_to') != 'null' else None) or local_result.date_to
        assignee = data.get('assignee') if data.get('assignee') not in {None, 'null', ''} else local_result.assignee
        assignee_scope = (data.get('assignee_scope') or local_result.assignee_scope or 'duty').lower()
        if assignee_scope not in {'duty', 'all_oplot'}:
            assignee_scope = local_result.assignee_scope or 'duty'
        explicit_task_types = local_result.explicit_task_types or bool(self._normalize_task_types(data.get('task_types')))
        scope = (data.get('scope') or 'anywhere').lower()
        search_text = self._normalize_ai_search_text(data.get('search_text'))
        confidence = (data.get('confidence') or 'medium').lower()

        summary_keywords = list(local_result.summary_keywords)
        description_keywords = list(local_result.description_keywords)
        keywords = list(local_result.keywords)
        label_filters = list(local_result.label_filters)

        if search_text:
            if scope == 'summary':
                summary_keywords = [search_text]
                description_keywords = []
                keywords = []
            elif scope == 'description':
                description_keywords = [search_text]
                summary_keywords = []
                keywords = []
            elif scope == 'labels':
                normalized_type = self._map_text_to_task_type(search_text)
                if normalized_type:
                    label_filters = [normalized_type]
                    task_types = [normalized_type]
                else:
                    keywords = [search_text]
                    summary_keywords = []
                    description_keywords = []
            else:
                keywords = [search_text]
                summary_keywords = []
                description_keywords = []

        if (
            task_types == local_result.task_types and
            status == local_result.status and
            date_from == local_result.date_from and
            date_to == local_result.date_to and
            assignee == local_result.assignee and
            assignee_scope == local_result.assignee_scope and
            explicit_task_types == local_result.explicit_task_types and
            keywords == local_result.keywords and
            summary_keywords == local_result.summary_keywords and
            description_keywords == local_result.description_keywords and
            label_filters == local_result.label_filters
        ):
            return None

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
            original_query=message,
            ai_confidence=confidence,
            assignee_scope=assignee_scope,
            explicit_task_types=explicit_task_types
        )

    def _extract_assignee_scope(self, message: str) -> str:
        """Определяет, нужно ли искать только по дежурным или по всем в OPLOT."""
        message_lower = message.lower()
        all_oplot_patterns = [
            r'по\s+всем\s+(?:людям|пользователям|сотрудникам|исполнителям)',
            r'среди\s+всех\s+(?:людей|пользователей|сотрудников|исполнителей)',
            r'по\s+всем\s+из\s+oplot',
            r'по\s+всей\s+группе\s+oplot',
            r'по\s+всем\s+в\s+oplot',
            r'по\s+всему\s+oplot',
            r'поиск\s+по\s+всем',
        ]
        if any(re.search(pattern, message_lower, re.IGNORECASE) for pattern in all_oplot_patterns):
            return 'all_oplot'
        return 'duty'

    def _normalize_task_types(self, raw_task_types) -> List[str]:
        if not isinstance(raw_task_types, list):
            return []
        normalized = []
        for item in raw_task_types:
            mapped = self._map_text_to_task_type(str(item))
            if mapped:
                normalized.append(mapped)
        return list(dict.fromkeys(normalized))

    def _map_text_to_task_type(self, text: str) -> Optional[str]:
        value = text.strip().lower()
        for task_type, aliases in self.TASK_TYPE_MAPPINGS.items():
            if value == task_type or value in aliases:
                return task_type
        return None

    def _normalize_ai_search_text(self, text: Optional[str]) -> Optional[str]:
        if not text or text == 'null':
            return None
        cleaned = str(text).strip().strip('"').strip("'").strip()
        cleaned = re.sub(r'^(?:слово|текст|значение|ключевое слово|ключевому слову)\s+', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -:,.')
        return cleaned or None

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

        relative_match = re.search(
            r'за\s+(?:последн(?:ие|ий|юю)\s+)?(\d+)\s*(дн(?:я|ей)?|сут(?:ки|ок)?|недел(?:ю|и|ь)?|месяц(?:а|ев)?|квартал(?:а|ов)?)',
            message,
            re.IGNORECASE
        )
        if relative_match:
            amount = int(relative_match.group(1))
            unit = relative_match.group(2).lower()
            if unit.startswith(('дн', 'сут')):
                delta_days = amount
            elif unit.startswith('недел'):
                delta_days = amount * 7
            elif unit.startswith('месяц'):
                delta_days = amount * 30
            elif unit.startswith('квартал'):
                delta_days = amount * 90
            else:
                delta_days = None

            if delta_days is not None:
                date_from = (today - timedelta(days=delta_days)).strftime('%Y-%m-%d')
                return date_from, date_to
        
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
            'с', 'по', 'за', 'на', 'в', 'какие', 'какой', 'касаемую', 'которые', 'который', 'которого', 'которых', 'которой',
            'текущий', 'текущие', 'день', 'дня', 'дней', 'сутки', 'суток',
            'вчера', 'сегодня', 'завтра', 'последний', 'последние', 'последняя',
            'неделя', 'неделю', 'недели', 'недель', 'месяц', 'месяца', 'месяцев', 'год', 'года', 'лет', 'квартал', 'квартала', 'кварталов',
            'двое', 'трое', 'четверо', 'открытые', 'закрытые', 'активные', 'выполненные',
            'текст', 'тексте', 'текстом', 'описание', 'описании', 'заголовок', 'заголовке',
            'содержит', 'содержат', 'содержащие', 'присутствует', 'присутсвует', 'присутвует', 'есть',
            'тег', 'тега', 'тегом', 'теги', 'ярлык', 'ярлыка', 'ярлыком', 'label', 'labels',
            'сотрудникам', 'сотрудники', 'статистика', 'статистику', 'сататистика', 'сататистику',
            'словом', 'слово', 'слову', 'слова', 'ключевому', 'ключевое', 'ключевосу', 'значением',
            'sup', 'logi', 'psi', 'db', 'infra', 'role',
        }
        words = re.findall(r'[\w.\-]+', message.lower())
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
            summary_inline = re.search(r'(?:слово|текст|значение|есть|присут\w*|содерж\w*)\s+(.+?)\s+в\s+(?:заголовк\w*|теме)', message, re.IGNORECASE)
            summary_source = self._extract_tail_after_markers(
                message,
                [r'в заголовк\w*', r'в теме', r'по заголовк\w*', r'summary']
            )
            summary_keywords = quoted_values or self._extract_scope_terms((summary_inline.group(1) if summary_inline else '') or summary_source or message)
        if any(word in message_lower for word in ['текст', 'описани', 'description']):
            description_inline = re.search(r'(?:слово|текст|значение|есть|присут\w*|содерж\w*)\s+(.+?)\s+в\s+описан\w*', message, re.IGNORECASE)
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
            r'\b(которые|который|которого|которых|которая|содержат|содержит|содержащие|присутствует|присутсвует|присутвует|есть|по ключевому слову|ключевому|ключевое|слову|слово|текст|значение|нужный|нужное)\b',
            ' ',
            text,
            flags=re.IGNORECASE
        )
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -:,.')
        if not cleaned:
            return []
        if '.' in cleaned:
            return [cleaned]
        if ' ' in cleaned or len(cleaned) <= 3:
            return [cleaned]
        return self._extract_keywords(cleaned)

    def _extract_unscoped_content_terms(self, message: str) -> List[str]:
        """Извлекает поисковую фразу, если пользователь просит искать слово/текст без указания области."""
        match = re.search(
            r'(?:по\s+ключев\w*\s+слов\w*|содерж\w*\s+(?:слово|текст|значение)|присут\w*|есть|со\s+словом)\s*(?:[:-]\s*)?(.+)$',
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
        if re.search(r'\b(?:тег\w*|ярлык\w*|label\w*)\b', message_lower, re.IGNORECASE):
            for task_type, pattern in self.TASK_TYPE_PATTERNS.items():
                if pattern.search(message_lower):
                    label_filters.append(task_type)

            for task_type, keywords in self.TASK_TYPE_MAPPINGS.items():
                if any(keyword.lower() in message_lower for keyword in keywords):
                    label_filters.append(task_type)
        return list(dict.fromkeys(label_filters))
    
    def build_jql(self, query: SearchQuery, date_field: str = 'created') -> str:
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
                sup_label_conditions = self._build_label_conditions('суп')
                type_conditions.append(
                    f'({sup_label_conditions} OR {sup_summary_conditions} OR {sup_value_conditions})'
                )
            elif task_type == 'логи':
                # Логи: labels ИЛИ summary
                logi_label_conditions = self._build_label_conditions('логи')
                type_conditions.append(
                    f'({logi_label_conditions} OR summary ~ "Логи" OR summary ~ "логи" OR summary ~ "Выгрузка логов" OR summary ~ "Выгрузить логи")'
                )
            elif task_type == 'пси':
                # ПСИ: labels
                type_conditions.append(f'({self._build_label_conditions("пси")})')
            elif task_type == 'внедрение':
                # Внедрение: labels
                type_conditions.append(f'({self._build_label_conditions("внедрение")})')
            elif task_type == 'бд':
                # БД: как на рабочем столе дежурного
                db_summary_conditions = ' OR '.join([f'summary ~ "{variant}"' for variant in DB_PATTERNS])
                type_conditions.append(
                    f'({self._build_label_conditions("бд")} OR {db_summary_conditions})'
                )
            elif task_type == 'инфра':
                # Инфра: как на рабочем столе дежурного
                infra_summary_conditions = ' OR '.join([f'summary ~ "{variant}"' for variant in INFRA_PATTERNS])
                type_conditions.append(
                    f'({self._build_label_conditions("инфра")} OR {infra_summary_conditions})'
                )
            elif task_type == 'роль':
                # Роль: как на рабочем столе дежурного
                role_summary_conditions = ' OR '.join([f'summary ~ "{variant}"' for variant in ROLE_PATTERNS])
                type_conditions.append(
                    f'({self._build_label_conditions("роль")} OR {role_summary_conditions})'
                )
        
        skip_default_type_filter = (
            query.assignee_scope == 'all_oplot' and
            not query.explicit_task_types and
            not query.label_filters
        )

        if type_conditions and not skip_default_type_filter:
            jql_parts.append(f'({" OR ".join(type_conditions)})')
        
        # Статус
        if query.status == 'open':
            jql_parts.append('status NOT IN (Done, Closed, Resolved)')
        elif query.status == 'closed':
            jql_parts.append('status IN (Done, Closed, Resolved)')
        
        # Даты - используем формат с временем для включения всего дня
        if query.date_from:
            jql_parts.append(f'{date_field} >= "{query.date_from} 00:00"')
        if query.date_to:
            jql_parts.append(f'{date_field} <= "{query.date_to} 23:59"')
        
        # Исполнитель
        if query.assignee:
            jql_parts.append(f'assignee ~ "{query.assignee}"')
        elif query.assignee_scope != 'all_oplot':
            assignees_filter = ', '.join([f'"{name}"' for name in DASHBOARD_ASSIGNEES])
            jql_parts.append(f'assignee IN ({assignees_filter})')
        
        if query.label_filters:
            label_conditions = []
            for task_type in query.label_filters:
                if task_type in self.LABEL_VARIANTS:
                    label_conditions.append(self._build_label_conditions(task_type))
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

        return ' AND '.join(jql_parts) + f' ORDER BY {date_field} DESC'

    def _build_label_conditions(self, task_type: str) -> str:
        variants = self.LABEL_VARIANTS.get(task_type, [])
        return ' OR '.join([f'labels = "{variant}"' for variant in variants])
    
    def execute_search(self, query: SearchQuery, date_field: str = 'created') -> List[Dict]:
        """Выполняет поиск задач по JQL"""
        jql = self.build_jql(query, date_field=date_field)
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
            visible_open = open_tasks[:20]
            hidden_open = open_tasks[20:]
            for task in visible_open:
                labels = ', '.join(task.get('labels', [])[:3]) or 'без тегов'
                text += f"• [{task['key']}]({task['url']}) - {self._escape_markdown(task['summary'][:70])}{'...' if len(task['summary']) > 70 else ''}\n"
                text += f"  👤 {self._escape_markdown(task['assignee_name'][:30])} | 🏷️ {self._escape_markdown(labels)}\n"
            if hidden_open:
                text += self._format_expandable_tasks(hidden_open, f"Показать еще {len(hidden_open)} открытых задач")
            text += "\n"

        if closed_tasks:
            text += f"✅ *Закрытые ({len(closed_tasks)}):*\n"
            visible_closed = closed_tasks[:20]
            hidden_closed = closed_tasks[20:]
            for task in visible_closed:
                labels = ', '.join(task.get('labels', [])[:3]) or 'без тегов'
                text += f"• [{task['key']}]({task['url']}) - {self._escape_markdown(task['summary'][:70])}{'...' if len(task['summary']) > 70 else ''}\n"
                text += f"  👤 {self._escape_markdown(task['assignee_name'][:30])} | 🏷️ {self._escape_markdown(labels)}\n"
            if hidden_closed:
                text += self._format_expandable_tasks(hidden_closed, f"Показать еще {len(hidden_closed)} закрытых задач")

        return text

    def _format_expandable_tasks(self, tasks: List[Dict], summary: str) -> str:
        """Формирует сворачиваемый блок со скрытой частью списка задач."""
        lines = [f"[details={summary}]"]
        for task in tasks:
            labels = ', '.join(task.get('labels', [])[:3]) or 'без тегов'
            lines.append(
                f"• [{task['key']}]({task['url']}) - "
                f"{self._escape_markdown(task['summary'][:70])}"
                f"{'...' if len(task['summary']) > 70 else ''}"
            )
            lines.append(
                f"  👤 {self._escape_markdown(task['assignee_name'][:30])} | "
                f"🏷️ {self._escape_markdown(labels)}"
            )
        lines.append("[/details]")
        return "\n".join(lines) + "\n"
    
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
                'assignee_scope': query.assignee_scope,
                'explicit_task_types': query.explicit_task_types,
                'ai_confidence': query.ai_confidence,
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
