import json
import logging
import requests
import threading
import time
import re
from datetime import datetime, timedelta
from collections import defaultdict

from config import TOKENS, DASHBOARD_ASSIGNEES, DASHBOARD_DAYS_BACK, DASHBOARD_CACHE_TTL

# Глобальные переменные для кэширования
_cache_lock = threading.Lock()
_cached_data = None
_last_cache_update = None

# Константы для тегов (возможные варианты регистра)
TAG_SUP_VARIANTS = ["СУП", "суп", "Суп"]
TAG_LOGI_VARIANTS = ["Логи", "логи", "ЛОГИ"]  # Все варианты регистра
TAG_VNEDRENIE_VARIANTS = ["Внедрение", "внедрение"]
TAG_ROLE_VARIANTS = ["роль", "Роль", "РОЛЬ"]
TAG_PSI_VARIANTS = ["ПСИ", "пси", "Пси"]

# Паттерны для поиска в тексте (регистронезависимые)
SUP_PATTERN = re.compile(r'СУП', re.IGNORECASE)
LOGI_PATTERN = re.compile(r'логи', re.IGNORECASE)

# Паттерны для раскаток ПСИ (текст в теме)
PSI_PATTERNS = [
    "Раскатить сборку Фокус",
    "Раскатить сборку",
    "Раскатка на ПСИ",
    "раскатить дистрибутив"
]
PSI_PATTERN = re.compile(r'(' + '|'.join(map(re.escape, PSI_PATTERNS)) + r')', re.IGNORECASE)

# Паттерны для определения типов задач (только по тексту, без тегов)
DB_PATTERNS = ["запрос к БД", "выгрузку из БД", "БД"]
INFRA_PATTERNS = ["ПОД", "перезагрузить под", "рестартануть под", "Работы по", "работы по"]
ROLE_PATTERNS = ["сменить роль", "добавить роль"]

# Компилированные регекс-паттерны для производительности
DB_PATTERN = re.compile(r'(' + '|'.join(map(re.escape, DB_PATTERNS)) + r')', re.IGNORECASE)
INFRA_PATTERN = re.compile(r'(' + '|'.join(map(re.escape, INFRA_PATTERNS)) + r')', re.IGNORECASE)
ROLE_PATTERN = re.compile(r'(' + '|'.join(map(re.escape, ROLE_PATTERNS)) + r')', re.IGNORECASE)

def get_jira_domain_and_token():
    """Возвращает домен и токен для Jira Delta"""
    return "https://jira.delta.sbrf.ru", TOKENS["delta_token"]

def check_tag_in_labels(labels, tag_variants):
    """Проверяет наличие тега в списке (регистронезависимо)"""
    labels_lower = [l.lower() for l in labels]
    return any(variant.lower() in labels_lower for variant in tag_variants)

def fetch_jira_tasks():
    """
    Получает задачи из Jira:
    1. СУП: по всему проекту OPLOT (тег ИЛИ текст в summary), за 30 дней
    2. Логи и операции: по всему проекту OPLOT (тег ИЛИ текст в summary/description), за 30 дней
    3. Задачи дежурных: ВСЕ активные задачи (любой давности), кроме Done/Closed
    """
    domain, token = get_jira_domain_and_token()
    
    excluded_statuses = ['Done', 'Closed', 'Resolved']
    statuses_filter = ', '.join([f'"{s}"' for s in excluded_statuses])
    start_date = (datetime.now() - timedelta(days=DASHBOARD_DAYS_BACK)).strftime("%Y-%m-%d")
    assignees_filter = ', '.join([f'"{name}"' for name in DASHBOARD_ASSIGNEES])
    
    # === ЗАПРОС 1: СУП задачи (по всему проекту, за 30 дней) ===
    jql_sup = (
        f'project = OPLOT AND '
        f'(labels = "СУП" OR summary ~ "СУП") AND '
        f'created >= "{start_date}" AND '
        f'status NOT IN ({statuses_filter}) '
        f'ORDER BY priority DESC, created DESC'
    )
    
    # === ЗАПРОС 2: Логи задачи (по всему проекту, за 30 дней) ===
    jql_logi = (
        f'project = OPLOT AND '
        f'(labels = "Логи" OR labels = "логи" OR labels = "ЛОГИ" OR '
        f'summary ~ "Логи" OR summary ~ "логи") AND '
        f'created >= "{start_date}" AND '
        f'status NOT IN ({statuses_filter}) '
        f'ORDER BY priority DESC, created DESC'
    )
    
    # === ЗАПРОС 3: ВСЕ активные задачи дежурных ===
    jql_assignee_all = (
        f'project = OPLOT AND '
        f'assignee IN ({assignees_filter}) AND '
        f'status NOT IN ({statuses_filter}) '
        f'ORDER BY priority DESC, created DESC'
    )
    
    # === ЗАПРОС 4: Задачи с операциями (БД, Инфра, Роли) по всему проекту ===
    # Ищем по тегу БД или ключевым словам в summary (НЕ в description)
    jql_operations = (
        f'project = OPLOT AND '
        f'(labels = "БД" OR labels = "бд" OR '
        f'summary ~ "БД" OR summary ~ "бд" OR '
        f'summary ~ "ПОД" OR summary ~ "под" OR '
        f'summary ~ "работы по" OR summary ~ "Работы по" OR '
        f'summary ~ "роль" OR summary ~ "Роль") AND '
        f'created >= "{start_date}" AND '
        f'status NOT IN ({statuses_filter}) '
        f'ORDER BY priority DESC, created DESC'
    )
    
    # === ЗАПРОС 5: ПСИ задачи (раскатки) - глобально по всему проекту ===
    # Ищем по тегу ПСИ или паттернам раскаток в summary
    jql_psi = (
        f'project = OPLOT AND '
        f'(labels = "ПСИ" OR labels = "пси" OR '
        f'summary ~ "Раскатить сборку Фокус" OR '
        f'summary ~ "Раскатить сборку" OR '
        f'summary ~ "Раскатка на ПСИ" OR '
        f'summary ~ "раскатить дистрибутив") AND '
        f'created >= "{start_date}" AND '
        f'status NOT IN ({statuses_filter}) '
        f'ORDER BY priority DESC, created DESC'
    )
    
    all_issues = []
    processed_keys = set()
    
    try:
        # Получаем СУП задачи
        logging.info(f"Dashboard: Запрос СУП задач")
        sup_issues = _execute_jql_query(domain, token, jql_sup)
        logging.info(f"Dashboard: Получено СУП задач: {len(sup_issues)}")
        
        # Получаем Логи задачи
        logging.info(f"Dashboard: Запрос Логи задач")
        logi_issues = _execute_jql_query(domain, token, jql_logi)
        logging.info(f"Dashboard: Получено Логи задач: {len(logi_issues)}")
        
        # Получаем задачи дежурных
        logging.info(f"Dashboard: Запрос задач дежурных")
        assignee_issues = _execute_jql_query(domain, token, jql_assignee_all)
        logging.info(f"Dashboard: Получено задач дежурных: {len(assignee_issues)}")
        
        # Получаем задачи с операциями (БД, Инфра, Роли) по всему проекту
        logging.info(f"Dashboard: Запрос задач с операциями")
        operations_issues = _execute_jql_query(domain, token, jql_operations)
        logging.info(f"Dashboard: Получено задач с операциями: {len(operations_issues)}")
        
        # Получаем ПСИ задачи (раскатки) - глобально по всему проекту
        logging.info(f"Dashboard: Запрос ПСИ задач")
        psi_issues = _execute_jql_query(domain, token, jql_psi)
        logging.info(f"Dashboard: Получено ПСИ задач: {len(psi_issues)}")
        
        # Обрабатываем СУП задачи
        for issue in sup_issues:
            key = issue['key']
            processed_keys.add(key)
            
            issue_data = _transform_issue(issue, domain)
            labels = issue['fields'].get('labels', [])
            summary = issue['fields'].get('summary', '')
            
            # Проверяем СУП (регистронезависимо)
            has_sup_tag = check_tag_in_labels(labels, TAG_SUP_VARIANTS)
            has_sup_in_summary = bool(SUP_PATTERN.search(summary))
            
            issue_data['has_sup_tag'] = has_sup_tag or has_sup_in_summary
            issue_data['sup_detected_by'] = 'tag' if has_sup_tag else ('summary' if has_sup_in_summary else 'none')
            
            # Проверяем Логи (регистронезависимо)
            has_logi_tag = check_tag_in_labels(labels, TAG_LOGI_VARIANTS)
            has_logi_in_summary = bool(LOGI_PATTERN.search(summary))
            issue_data['has_logi_tag'] = has_logi_tag or has_logi_in_summary
            if issue_data['has_logi_tag']:
                issue_data['logi_detected_by'] = 'tag' if has_logi_tag else 'summary'
            
            # Проверяем Внедрение
            issue_data['has_vnedrenie_tag'] = check_tag_in_labels(labels, TAG_VNEDRENIE_VARIANTS)
            
            logging.debug(f"Task {key}: SUP={issue_data['has_sup_tag']}, LOGI={issue_data['has_logi_tag']}, VN={issue_data['has_vnedrenie_tag']}")
            
            all_issues.append(issue_data)
        
        # Обрабатываем Логи задачи
        for issue in logi_issues:
            key = issue['key']
            labels = issue['fields'].get('labels', [])
            summary = issue['fields'].get('summary', '')
            
            has_logi_tag = check_tag_in_labels(labels, TAG_LOGI_VARIANTS)
            has_logi_in_summary = bool(LOGI_PATTERN.search(summary))
            
            # Если задача уже была в СУП, обновляем флаги
            if key in processed_keys:
                for existing in all_issues:
                    if existing['key'] == key:
                        existing['has_logi_tag'] = True
                        existing['logi_detected_by'] = 'tag' if has_logi_tag else 'summary'
                        logging.debug(f"Updated existing task {key} with LOGI flag")
                        break
            else:
                processed_keys.add(key)
                
                issue_data = _transform_issue(issue, domain)
                
                has_sup_tag = check_tag_in_labels(labels, TAG_SUP_VARIANTS)
                has_sup_in_summary = bool(SUP_PATTERN.search(summary))
                
                issue_data['has_sup_tag'] = has_sup_tag or has_sup_in_summary
                issue_data['has_logi_tag'] = has_logi_tag or has_logi_in_summary
                issue_data['logi_detected_by'] = 'tag' if has_logi_tag else ('summary' if has_logi_in_summary else 'none')
                issue_data['has_vnedrenie_tag'] = check_tag_in_labels(labels, TAG_VNEDRENIE_VARIANTS)
                
                logging.debug(f"New LOGI task {key}: SUP={issue_data['has_sup_tag']}, LOGI={issue_data['has_logi_tag']}")
                
                all_issues.append(issue_data)
        
        # Обрабатываем задачи дежурных
        for issue in assignee_issues:
            key = issue['key']
            labels = issue['fields'].get('labels', [])
            summary = issue['fields'].get('summary', '')
            
            has_sup_tag = check_tag_in_labels(labels, TAG_SUP_VARIANTS)
            has_sup_in_summary = bool(SUP_PATTERN.search(summary))
            has_logi_tag = check_tag_in_labels(labels, TAG_LOGI_VARIANTS)
            has_logi_in_summary = bool(LOGI_PATTERN.search(summary))
            
            # Если задача уже есть, обновляем флаги
            if key in processed_keys:
                for existing in all_issues:
                    if existing['key'] == key:
                        existing['has_sup_tag'] = existing.get('has_sup_tag') or has_sup_tag or has_sup_in_summary
                        existing['has_logi_tag'] = existing.get('has_logi_tag') or has_logi_tag or has_logi_in_summary
                        existing['has_vnedrenie_tag'] = existing.get('has_vnedrenie_tag') or check_tag_in_labels(labels, TAG_VNEDRENIE_VARIANTS)
                        break
            else:
                processed_keys.add(key)
                
                issue_data = _transform_issue(issue, domain)
                
                issue_data['has_sup_tag'] = has_sup_tag or has_sup_in_summary
                issue_data['has_logi_tag'] = has_logi_tag or has_logi_in_summary
                issue_data['has_vnedrenie_tag'] = check_tag_in_labels(labels, TAG_VNEDRENIE_VARIANTS)
                
                all_issues.append(issue_data)
        
        # Обрабатываем задачи с операциями (БД, Инфра, Роли) - по всему проекту
        for issue in operations_issues:
            key = issue['key']
            
            # Если задача уже есть, пропускаем (флаги уже установлены)
            if key in processed_keys:
                continue
            
            processed_keys.add(key)
            
            issue_data = _transform_issue(issue, domain)
            labels = issue['fields'].get('labels', [])
            summary = issue['fields'].get('summary', '')
            
            # Проверяем все типы
            has_sup_tag = check_tag_in_labels(labels, TAG_SUP_VARIANTS)
            has_sup_in_summary = bool(SUP_PATTERN.search(summary))
            has_logi_tag = check_tag_in_labels(labels, TAG_LOGI_VARIANTS)
            has_logi_in_summary = bool(LOGI_PATTERN.search(summary))
            
            issue_data['has_sup_tag'] = has_sup_tag or has_sup_in_summary
            issue_data['has_logi_tag'] = has_logi_tag or has_logi_in_summary
            issue_data['has_vnedrenie_tag'] = check_tag_in_labels(labels, TAG_VNEDRENIE_VARIANTS)
            
            all_issues.append(issue_data)
        
        # Обрабатываем ПСИ задачи (раскатки) - глобально по всему проекту
        for issue in psi_issues:
            key = issue['key']
            labels = issue['fields'].get('labels', [])
            summary = issue['fields'].get('summary', '')
            
            # Если задача уже есть, обновляем флаги ПСИ
            if key in processed_keys:
                for existing in all_issues:
                    if existing['key'] == key:
                        has_psi_tag = check_tag_in_labels(labels, TAG_PSI_VARIANTS)
                        has_psi_text = bool(PSI_PATTERN.search(summary))
                        existing['has_psi_tag'] = has_psi_tag
                        existing['is_psi_task'] = has_psi_tag or has_psi_text
                        existing['psi_detected_by'] = 'tag' if has_psi_tag else ('summary' if has_psi_text else None)
                        break
            else:
                processed_keys.add(key)
                
                issue_data = _transform_issue(issue, domain)
                
                # Проверяем все типы
                has_sup_tag = check_tag_in_labels(labels, TAG_SUP_VARIANTS)
                has_sup_in_summary = bool(SUP_PATTERN.search(summary))
                has_logi_tag = check_tag_in_labels(labels, TAG_LOGI_VARIANTS)
                has_logi_in_summary = bool(LOGI_PATTERN.search(summary))
                has_psi_tag = check_tag_in_labels(labels, TAG_PSI_VARIANTS)
                has_psi_text = bool(PSI_PATTERN.search(summary))
                
                issue_data['has_sup_tag'] = has_sup_tag or has_sup_in_summary
                issue_data['has_logi_tag'] = has_logi_tag or has_logi_in_summary
                issue_data['has_vnedrenie_tag'] = check_tag_in_labels(labels, TAG_VNEDRENIE_VARIANTS)
                issue_data['has_psi_tag'] = has_psi_tag
                issue_data['is_psi_task'] = has_psi_tag or has_psi_text
                issue_data['psi_detected_by'] = 'tag' if has_psi_tag else ('summary' if has_psi_text else None)
                
                all_issues.append(issue_data)
        
        # ЛОГИРОВАНИЕ для отладки
        logi_count = sum(1 for i in all_issues if i['has_logi_tag'])
        sup_count = sum(1 for i in all_issues if i['has_sup_tag'])
        db_count = sum(1 for i in all_issues if i['has_db_tag'])
        infra_count = sum(1 for i in all_issues if i['has_infra_tag'])
        role_count = sum(1 for i in all_issues if i['has_role_tag'])
        psi_count = sum(1 for i in all_issues if i.get('is_psi_task'))
        logging.info(f"Dashboard: Итого задач: СУП={sup_count}, Логи={logi_count}, БД={db_count}, Инфра={infra_count}, Роли={role_count}, ПСИ={psi_count}, всего уникальных={len(all_issues)}")
        
    except Exception as e:
        logging.error(f"Dashboard: Ошибка при запросе к Jira: {e}")
        raise
    
    return all_issues

def _execute_jql_query(domain, token, jql):
    """Выполняет JQL запрос с пагинацией"""
    all_results = []
    start_at = 0
    max_per_request = 100
    
    logging.debug(f"Executing JQL: {jql}")
    
    while True:
        url = f"{domain}/rest/api/2/search"
        params = {
            'jql': jql,
            'startAt': start_at,
            'maxResults': max_per_request,
            'fields': 'key,summary,created,updated,status,assignee,reporter,labels,priority,issuetype,description,duedate',
        }
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        response = requests.get(url, headers=headers, params=params, verify=False, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        issues = data.get('issues', [])
        total = data.get('total', 0)
        
        if not issues:
            break
        
        all_results.extend(issues)
        start_at += len(issues)
        
        if start_at >= total or len(issues) < max_per_request:
            break
    
    return all_results

def detect_task_types(issue):
    """
    Определяет типы задачи по тегам и тексту summary
    БД ищем только по тегу "БД" или слову "БД" в заголовке
    Инфра и роли ищем по тегам или тексту summary
    ПСИ ищем по тегу "ПСИ" или тексту раскатки в summary
    Возвращает словарь с флагами типов
    """
    labels = issue['fields'].get('labels', [])
    summary = issue['fields'].get('summary', '')
    
    # Проверяем теги
    has_logi_tag = check_tag_in_labels(labels, TAG_LOGI_VARIANTS)
    has_role_tag = check_tag_in_labels(labels, TAG_ROLE_VARIANTS)
    # Проверяем тег БД (регистронезависимо)
    has_db_tag = any(label.lower() == 'бд' for label in labels)
    # Проверяем тег ПСИ
    has_psi_tag = check_tag_in_labels(labels, TAG_PSI_VARIANTS)
    
    # Проверяем текст только в summary (не в description)
    # Логи - только в summary
    has_logi_text = bool(LOGI_PATTERN.search(summary))
    has_logi = has_logi_tag or has_logi_text
    
    # БД - только по тегу или слову "БД" в заголовке
    has_db_in_summary = bool(re.search(r'\bБД\b', summary, re.IGNORECASE))
    has_db = has_db_tag or has_db_in_summary
    
    # Инфра / Рестарт - в summary
    has_infra_text = bool(INFRA_PATTERN.search(summary))
    
    # Роли - по тегу или тексту summary
    has_role_text = bool(ROLE_PATTERN.search(summary))
    has_role = has_role_tag or has_role_text
    
    # ПСИ - по тегу или паттернам раскаток в summary
    has_psi_text = bool(PSI_PATTERN.search(summary))
    is_psi_task = has_psi_tag or has_psi_text
    
    return {
        'has_logi_tag': has_logi,
        'has_db_tag': has_db,
        'has_infra_tag': has_infra_text,
        'has_role_tag': has_role,
        'has_psi_tag': has_psi_tag,  # Только по тегу ПСИ
        'is_psi_task': is_psi_task,   # По тегу или тексту
        # Дополнительно: источник определения
        'logi_detected_by': 'tag' if has_logi_tag else ('summary' if has_logi_text else None),
        'role_detected_by': 'tag' if has_role_tag else ('summary' if has_role_text else None),
        'db_detected_by': 'tag' if has_db_tag else ('summary' if has_db_in_summary else None),
        'infra_detected_by': 'summary' if has_infra_text else None,
        'psi_detected_by': 'tag' if has_psi_tag else ('summary' if has_psi_text else None),
    }

def _transform_issue(issue, domain):
    """Трансформирует сырые данные Jira в наш формат"""
    assignee = issue['fields'].get('assignee')
    assignee_name = assignee.get('displayName', 'Не назначен') if assignee else 'Не назначен'
    
    # Определяем типы задачи
    task_types = detect_task_types(issue)
    
    return {
        'key': issue['key'],
        'summary': issue['fields'].get('summary', ''),
        'description': issue['fields'].get('description', '') or '',
        'created': issue['fields'].get('created', ''),
        'updated': issue['fields'].get('updated', ''),
        'status': issue['fields'].get('status', {}).get('name', ''),
        'assignee_name': assignee_name,
        'assignee_avatar': assignee.get('avatarUrls', {}).get('48x48', '') if assignee else '',
        'reporter': issue['fields'].get('reporter', {}).get('displayName', '') if issue['fields'].get('reporter') else '',
        'labels': issue['fields'].get('labels', []),
        'priority': issue['fields'].get('priority', {}).get('name', ''),
        'priority_icon': issue['fields'].get('priority', {}).get('iconUrl', ''),
        'issue_type': issue['fields'].get('issuetype', {}).get('name', ''),
        'issue_type_icon': issue['fields'].get('issuetype', {}).get('iconUrl', ''),
        'url': f"{domain}/browse/{issue['key']}",
        'has_sup_tag': False,
        'has_vnedrenie_tag': False,
        'days_in_progress': calculate_days_in_progress(issue['fields'].get('created')),
        # Новые поля типов задач
        **task_types,
        # Поле для согласования (заполняется отдельно)
        'is_approved': False,
        'approval_checked': False
    }

def calculate_days_in_progress(created_date):
    """Вычисляет сколько дней задача существует"""
    if not created_date:
        return 0
    
    try:
        created = datetime.strptime(created_date[:10], "%Y-%m-%d")
        delta = datetime.now() - created
        return max(0, delta.days)
    except:
        return 0

def process_tasks_data(issues):
    """
    Обрабатывает данные:
    1. СУП задачи - ВСЕ из проекта, за 30 дней
    2. Логи задачи - ВСЕ из проекта, за 30 дней (включая те что и СУП)
    3. Внедрение ПРОМ - по дежурным с тегом Внедрение
    4. Внедрение ПСИ - глобально по всему проекту (тег ПСИ или текст раскатки)
    5. Структура по дежурным - ВСЕ их активные задачи
    """
    sup_tasks = []
    logi_tasks = []
    vnedrenie_prom_tasks = []
    vnedrenie_psi_tasks = []
    
    assignee_stats = {name: {'todo': [], 'in_progress': [], 'stale_count': 0}
                      for name in DASHBOARD_ASSIGNEES}
    
    cutoff_date = datetime.now() - timedelta(days=DASHBOARD_DAYS_BACK)
    
    for issue in issues:
        assignee = issue['assignee_name']
        is_our_assignee = assignee in DASHBOARD_ASSIGNEES
        
        # === СУП ЗАДАЧИ ===
        if issue['has_sup_tag']:
            try:
                created = datetime.strptime(issue['created'][:10], "%Y-%m-%d")
                if created >= cutoff_date:
                    sup_tasks.append(issue)
            except:
                pass
        
        # === ЛОГИ И ОПЕРАЦИОННЫЕ ЗАДАЧИ ===
        # Включаем задачи с тегами: логи, бд, инфра, роли
        is_operation_task = (issue['has_logi_tag'] or
                            issue['has_db_tag'] or
                            issue['has_infra_tag'] or
                            issue['has_role_tag'])
        
        if is_operation_task:
            try:
                created = datetime.strptime(issue['created'][:10], "%Y-%m-%d")
                if created >= cutoff_date:
                    logi_tasks.append(issue)
                    logging.debug(f"Added to logi_tasks: {issue['key']} (logi={issue['has_logi_tag']}, db={issue['has_db_tag']}, infra={issue['has_infra_tag']}, role={issue['has_role_tag']})")
            except Exception as e:
                logging.warning(f"Error processing logi task {issue['key']}: {e}")
        
        # === ВНЕДРЕНИЕ ПРОМ ===
        # Только по дежурным с тегом Внедрение
        if issue['has_vnedrenie_tag'] and is_our_assignee:
            vnedrenie_prom_tasks.append(issue)
        
        # === ВНЕДРЕНИЕ ПСИ ===
        # Глобально по всему проекту (тег ПСИ или паттерны раскаток в summary)
        if issue.get('is_psi_task'):
            try:
                created = datetime.strptime(issue['created'][:10], "%Y-%m-%d")
                if created >= cutoff_date:
                    vnedrenie_psi_tasks.append(issue)
                    logging.debug(f"Added to vnedrenie_psi_tasks: {issue['key']}")
            except Exception as e:
                logging.warning(f"Error processing psi task {issue['key']}: {e}")
        
        # === СТРУКТУРА ПО ДЕЖУРНЫМ ===
        if is_our_assignee:
            is_stale = issue['days_in_progress'] > 7
            status = issue['status']
            
            is_todo = status in ['To Do', 'Open', 'New', 'Backlog', 'Сделать']
            is_in_progress = status in ['In Progress', 'В работе', 'Progress', 'Development']
            
            if is_todo:
                assignee_stats[assignee]['todo'].append(issue)
                if is_stale:
                    assignee_stats[assignee]['stale_count'] += 1
                    
            elif is_in_progress:
                assignee_stats[assignee]['in_progress'].append(issue)
                if is_stale:
                    assignee_stats[assignee]['stale_count'] += 1
    
    # Сортировка
    priority_order = {'Highest': 0, 'High': 1, 'Critical': 1, 'Medium': 2, 'Low': 3, 'Lowest': 4}
    
    def sort_key(task):
        p = task.get('priority', '')
        return (priority_order.get(p, 5), task.get('created', ''))
    
    sup_tasks.sort(key=sort_key)
    logi_tasks.sort(key=sort_key)
    vnedrenie_prom_tasks.sort(key=sort_key)
    vnedrenie_psi_tasks.sort(key=sort_key)
    
    for assignee in assignee_stats:
        assignee_stats[assignee]['todo'].sort(key=sort_key, reverse=True)
        assignee_stats[assignee]['in_progress'].sort(key=sort_key, reverse=True)
    
    logging.info(f"process_tasks_data: СУП={len(sup_tasks)}, Логи={len(logi_tasks)}, Внедрение ПРОМ={len(vnedrenie_prom_tasks)}, Внедрение ПСИ={len(vnedrenie_psi_tasks)}")
    
    return {
        'sup_tasks': sup_tasks,
        'logi_tasks': logi_tasks,
        'vnedrenie_prom_tasks': vnedrenie_prom_tasks,
        'vnedrenie_psi_tasks': vnedrenie_psi_tasks,
        'assignee_stats': assignee_stats,
        'dashboard_assignees': DASHBOARD_ASSIGNEES
    }

def get_dashboard_data():
    """Получает данные для дашборда с кэшированием"""
    global _cached_data, _last_cache_update
    
    with _cache_lock:
        now = time.time()
        
        if (_cached_data is not None and 
            _last_cache_update is not None and 
            (now - _last_cache_update) < DASHBOARD_CACHE_TTL):
            logging.info(f"Dashboard: Кэш (возраст: {int(now - _last_cache_update)} сек)")
            return _cached_data
        
        logging.info("Dashboard: Обновление кэша...")
        try:
            raw_issues = fetch_jira_tasks()
            processed_data = process_tasks_data(raw_issues)
            
            _cached_data = processed_data
            _last_cache_update = now
            
            total_assignee_tasks = sum(
                len(a['todo']) + len(a['in_progress']) 
                for a in processed_data['assignee_stats'].values()
            )
            
            logging.info(
                f"Dashboard: СУП={len(processed_data['sup_tasks'])}, "
                f"Логи={len(processed_data['logi_tasks'])}, "
                f"Внедрение ПРОМ={len(processed_data['vnedrenie_prom_tasks'])}, "
                f"Внедрение ПСИ={len(processed_data['vnedrenie_psi_tasks'])}, "
                f"Дежурные={total_assignee_tasks}"
            )
            return _cached_data
            
        except Exception as e:
            logging.error(f"Dashboard: Ошибка обновления кэша: {e}")
            if _cached_data is not None:
                logging.warning("Dashboard: Возвращаем устаревший кэш")
                return _cached_data
            raise

def force_refresh_cache():
    """Принудительное обновление кэша"""
    global _cached_data, _last_cache_update
    
    with _cache_lock:
        _cached_data = None
        _last_cache_update = None
    
    return get_dashboard_data()

# === Функции для проверки согласования ===

# Кэш для результатов проверки согласования (отдельный от основного кэша)
_approval_cache = {}
_approval_cache_ttl = 1800  # 30 минут

def check_issue_approval(issue_key):
    """
    Проверяет комментарии задачи на наличие слова 'согласовано'
    Возвращает True если найдено, иначе False
    """
    global _approval_cache
    
    # Проверяем кэш
    if issue_key in _approval_cache:
        cached_time, result = _approval_cache[issue_key]
        if time.time() - cached_time < _approval_cache_ttl:
            return result
    
    domain, token = get_jira_domain_and_token()
    
    try:
        url = f"{domain}/rest/api/2/issue/{issue_key}/comment"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        comments = data.get('comments', [])
        
        # Ищем "согласовано" в комментариях (без учета регистра)
        approval_pattern = re.compile(r'согласовано', re.IGNORECASE)
        
        for comment in comments:
            body = comment.get('body', '')
            if approval_pattern.search(body):
                # Сохраняем в кэш
                _approval_cache[issue_key] = (time.time(), True)
                return True
        
        # Сохраняем отрицательный результат в кэш
        _approval_cache[issue_key] = (time.time(), False)
        return False
        
    except Exception as e:
        logging.error(f"Error checking approval for {issue_key}: {e}")
        return False

def check_multiple_approvals(issue_keys, force_refresh=False):
    """
    Проверяет согласование для нескольких задач
    Возвращает словарь {issue_key: is_approved}
    
    Args:
        issue_keys: список ключей задач
        force_refresh: если True, игнорирует кэш и делает новый запрос
    """
    global _approval_cache
    
    # Если force_refresh - очищаем кэш для этих задач
    if force_refresh:
        for key in issue_keys:
            if key in _approval_cache:
                del _approval_cache[key]
    
    results = {}
    for key in issue_keys:
        results[key] = check_issue_approval(key)
    return results

def clear_approval_cache():
    """Очищает кэш согласований"""
    global _approval_cache
    _approval_cache = {}
    logging.info("Approval cache cleared")

def get_task_type_badges(task):
    """
    Возвращает список бейджей для задачи на основе её типов
    """
    badges = []
    
    if task.get('has_logi_tag'):
        badges.append({'type': 'logi', 'icon': '📝', 'label': 'Логи', 'class': 'badge-logi'})
    
    if task.get('has_db_tag'):
        badges.append({'type': 'db', 'icon': '🗄️', 'label': 'БД', 'class': 'badge-db'})
    
    if task.get('has_infra_tag'):
        badges.append({'type': 'infra', 'icon': '🔄', 'label': 'Инфра', 'class': 'badge-infra'})
    
    if task.get('has_role_tag'):
        badges.append({'type': 'role', 'icon': '👤', 'label': 'Роль', 'class': 'badge-role'})
    
    if task.get('is_approved'):
        badges.append({'type': 'approved', 'icon': '✅', 'label': 'Согласовано', 'class': 'badge-approved'})
    
    return badges