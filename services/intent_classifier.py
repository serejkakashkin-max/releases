"""
Классификатор намерений для чат-бота дашборда дежурного.
Определяет тип запроса пользователя на основе паттернов.
"""

import re
from enum import Enum
from typing import Dict, List, Tuple, Optional


class IntentType(Enum):
    """Типы намерений пользователя"""
    SEARCH_TASKS = "search_tasks"           # Поиск задач
    GENERATE_REPORT = "generate_report"     # Генерация отчёта
    SPECIFIC_TASK = "specific_task"         # Запрос о конкретной задаче
    GREETING = "greeting"                   # Приветствие
    SHOW_CAPABILITIES = "show_capabilities" # Показать возможности
    UNKNOWN = "unknown"                     # Неизвестное намерение


class IntentClassifier:
    """Классификатор намерений на основе паттернов и ключевых слов"""
    
    # Паттерны для каждого типа намерения
    INTENT_PATTERNS: Dict[IntentType, List[str]] = {
        IntentType.SEARCH_TASKS: [
            r'найди',
            r'покажи',
            r'поиск',
            r'где',
            r'задач[аиу].*заголов',
            r'задач[аиу].*текст',
            r'задач[аиу].*описан',
            r'задач[аиу].*тег',
            r'какие.*задачи',
            r'список.*задач',
            r'задачи.*по',
            r'все.*задачи',
            r'закрытые.*задачи',
            r'открытые.*задачи',
        ],
        IntentType.GENERATE_REPORT: [
            r'сводка',
            r'отч[её]т',
            r'передача смены',
            r'статистика',
            r'сататистик',
            r'сгенерир',
            r'сформир',
            r'итоги',
            r'подведи итог',
            r'что сделано',
            r'результаты',
            r'сформируй.*статистику',
            r'статистика.*сотрудник',
            r'по сотрудникам',
            r'закрытые задачи.*сотрудник',
            r'эффективность',
            r'производительность',
        ],
        IntentType.SPECIFIC_TASK: [
            r'OPLOT-\d+',
            r'SMECSC-\d+',
            r'SMEPG-\d+',
            r'EMRM-\d+',
            r'SMECLM-\d+',
        ],
        IntentType.GREETING: [
            r'привет',
            r'здравствуй',
            r'добрый день',
            r'доброе утро',
            r'добрый вечер',
            r'hi',
            r'hello',
        ],
        IntentType.SHOW_CAPABILITIES: [
            r'что ты умеешь',
            r'что ты можешь',
            r'покажи что ты умеешь',
            r'показать что я умею',
            r'покажи возможности',
            r'какие возможности',
            r'что ты можешь делать',
            r'help',
            r'помощь',
        ],
    }
    
    # Ключевые слова для уточнения типа задач
    TASK_TYPE_KEYWORDS = {
        'суп': ['суп', 'sup'],
        'логи': ['логи', 'logi', 'logs'],
        'пси': ['пси', 'psi'],
        'внедрение': ['внедрение', 'vnedrenie'],
        'роль': ['роль', 'role'],
        'бд': ['бд', 'db', 'база данных'],
        'инфра': ['инфра', 'infra', 'под', 'pod'],
    }
    
    # Паттерны для извлечения параметров отчёта
    DAYS_PATTERN = re.compile(r'(\d+)\s*(?:дней|дня|день|days?)', re.IGNORECASE)
    QUARTER_PATTERN = re.compile(r'(\d+)\s*квартал(?:\s*(\d{4}))?', re.IGNORECASE)
    
    def __init__(self):
        """Инициализация компилированных паттернов"""
        self.compiled_patterns: Dict[IntentType, List[re.Pattern]] = {}
        self._compile_patterns()
    
    def _compile_patterns(self):
        """Компилирует паттерны для производительности"""
        for intent, patterns in self.INTENT_PATTERNS.items():
            self.compiled_patterns[intent] = [
                re.compile(pattern, re.IGNORECASE) for pattern in patterns
            ]
    
    def classify(self, message: str) -> IntentType:
        """
        Классифицирует намерение пользователя.
        
        Args:
            message: Сообщение пользователя
            
        Returns:
            IntentType: Определённый тип намерения
        """
        message_lower = message.lower().strip()
        
        # Проверяем паттерны для каждого типа
        scores: Dict[IntentType, int] = {}
        
        for intent, patterns in self.compiled_patterns.items():
            score = 0
            for pattern in patterns:
                if pattern.search(message_lower):
                    score += 1
                    # Специфические задачи имеют высокий приоритет
                    if intent == IntentType.SPECIFIC_TASK:
                        return IntentType.SPECIFIC_TASK
            if score > 0:
                scores[intent] = score
        
        # Если ничего не найдено
        if not scores:
            return IntentType.UNKNOWN
        
        # Возвращаем тип с наибольшим score
        return max(scores, key=scores.get)
    
    def extract_task_key(self, message: str) -> Optional[str]:
        """
        Извлекает ключ задачи из сообщения.
        
        Args:
            message: Сообщение пользователя
            
        Returns:
            Optional[str]: Ключ задачи или None
        """
        patterns = [
            r'(OPLOT-\d+)',
            r'(SMECSC-\d+)',
            r'(SMEPG-\d+)',
            r'(EMRM-\d+)',
            r'(SMECLM-\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                return match.group(1).upper()
        
        return None
    
    def extract_report_params(self, message: str) -> Dict:
        """
        Извлекает параметры для отчёта (дни, квартал).
        
        Args:
            message: Сообщение пользователя
            
        Returns:
            Dict: {'days': int|None, 'quarter': int|None, 'year': int|None}
        """
        result = {
            'days': None,
            'quarter': None,
            'year': None
        }
        
        # Ищем количество дней
        days_match = self.DAYS_PATTERN.search(message)
        if days_match:
            result['days'] = int(days_match.group(1))

        message_lower = message.lower()
        if result['days'] is None:
            weeks_match = re.search(r'за\s+(\d+)\s+нед\w*', message_lower)
            if weeks_match:
                result['days'] = int(weeks_match.group(1)) * 7
            elif any(word in message_lower for word in ['за неделю', 'неделю', 'неделя', 'weekly']):
                result['days'] = 7
            elif any(word in message_lower for word in ['за месяц', 'месяц', 'месяца', 'monthly']):
                result['days'] = 30
            elif any(word in message_lower for word in ['за сутки', 'сутки', 'суток']):
                result['days'] = 1
            elif any(word in message_lower for word in ['за 2 недели', 'две недели']):
                result['days'] = 14
        
        # Ищем квартал и год
        quarter_match = self.QUARTER_PATTERN.search(message)
        if quarter_match:
            result['quarter'] = int(quarter_match.group(1))
            if quarter_match.group(2):
                result['year'] = int(quarter_match.group(2))
            else:
                # Если год не указан, используем текущий
                from datetime import datetime
                result['year'] = datetime.now().year
        
        return result
    
    def extract_search_params(self, message: str) -> Dict:
        """
        Извлекает параметры поиска из сообщения.
        
        Args:
            message: Сообщение пользователя
            
        Returns:
            Dict: Параметры поиска
        """
        params = {
            'text': None,
            'assignee': None,
            'status': 'all',  # по умолчанию все задачи
            'tags': [],
            'date_range': None,
        }
        
        message_lower = message.lower()
        
        # Определяем статус
        if any(word in message_lower for word in ['закрыт', 'завершен', 'выполнен', 'done', 'closed']):
            params['status'] = 'closed'
        elif any(word in message_lower for word in ['открыт', 'активен', 'в работе', 'open']):
            params['status'] = 'open'
        
        # Определяем теги
        for tag, keywords in self.TASK_TYPE_KEYWORDS.items():
            if any(keyword in message_lower for keyword in keywords):
                params['tags'].append(tag.upper())
        
        # Извлекаем ФИО (упрощённо)
        assignee_pattern = r'(?:задачи|от|у|для)\s+([А-Я][а-я]+\s+[А-Я]\.?[А-Я]?\.?)'
        match = re.search(assignee_pattern, message)
        if match:
            params['assignee'] = match.group(1)
        
        # Определяем временной диапазон
        if any(word in message_lower for word in ['сегодня', 'today']):
            params['date_range'] = 'today'
        elif any(word in message_lower for word in ['вчера', 'yesterday']):
            params['date_range'] = 'yesterday'
        elif any(word in message_lower for word in ['неделю', 'week', 'неделя']):
            params['date_range'] = 'week'
        elif any(word in message_lower for word in ['месяц', 'month', 'месяца']):
            params['date_range'] = 'month'
        
        return params
    
    def get_suggestions(self, intent: IntentType) -> List[str]:
        """
        Возвращает подсказки для следующего шага.
        
        Args:
            intent: Тип намерения
            
        Returns:
            List[str]: Список подсказок
        """
        suggestions = {
            IntentType.SEARCH_TASKS: [
                "Показать что я умею?",
                "Сгенерировать статистику",
                "Сводка для передачи дневной смены",
            ],
            IntentType.GENERATE_REPORT: [
                "Сгенерировать статистику",
                "Сводка для передачи дневной смены",
                "Сводка для передачи вечерней смены",
            ],
            IntentType.SHOW_CAPABILITIES: [
                "Сгенерировать статистику",
                "Сводка для передачи дневной смены",
                "Сводка для передачи вечерней смены",
            ],
            IntentType.UNKNOWN: [
                "Показать что я умею?",
                "Сгенерировать статистику",
                "Сводка для передачи дневной смены",
            ],
        }
        
        return suggestions.get(intent, suggestions[IntentType.UNKNOWN])


# Singleton для использования в приложении
_intent_classifier = None


def get_intent_classifier() -> IntentClassifier:
    """Возвращает singleton экземпляр классификатора"""
    global _intent_classifier
    if _intent_classifier is None:
        _intent_classifier = IntentClassifier()
    return _intent_classifier
