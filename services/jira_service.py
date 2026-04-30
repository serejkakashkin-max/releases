import re
import logging
import requests
import time
from datetime import datetime, timedelta
from config import TOKENS


def _iter_nested_values(value):
    if isinstance(value, dict):
        yield value
        for nested_value in value.values():
            yield from _iter_nested_values(nested_value)
    elif isinstance(value, list):
        for nested_item in value:
            yield from _iter_nested_values(nested_item)
    elif value is not None:
        yield value


def _format_ke_id(raw_ke_id):
    if not raw_ke_id:
        return ""

    raw_value = str(raw_ke_id).strip()
    if raw_value.upper().startswith("CI"):
        digits = re.sub(r"\D", "", raw_value[2:])
    else:
        digits = re.sub(r"\D", "", raw_value)

    if not digits:
        return ""
    return f"CI{digits.zfill(8)}"


def _extract_ke_from_distributive_field(raw_value):
    for value in _iter_nested_values(raw_value):
        if not isinstance(value, dict):
            continue

        for key in ("id", "smId", "PARENT_CI"):
            ke = _format_ke_id(value.get(key))
            if ke:
                return ke
    return ""


def _extract_version_from_distributive_field(raw_value):
    version_pattern = re.compile(r"[DP]-\d+\.\d+\.\d+-\d+")

    for value in _iter_nested_values(raw_value):
        if isinstance(value, dict):
            for key in ("version", "buildVersion", "release_version", "releases_version", "value", "url"):
                raw_version = value.get(key)
                if not raw_version:
                    continue

                match = version_pattern.search(str(raw_version))
                if match:
                    return match.group(0)
        else:
            match = version_pattern.search(str(value))
            if match:
                return match.group(0)

    return ""


def get_jira_domain_and_token(release_id):
    # ИЗМЕНЕНО: релизы из delta-домена
    delta_prefixes = ("SMECSC", "SMEPG", "HELPERAI", "AIGAS")
    if any(release_id.startswith(prefix) for prefix in delta_prefixes):
        return "https://jira.delta.sbrf.ru", TOKENS["delta_token"]
    return "https://jira.sberbank.ru", TOKENS["sberbank_token"]

def get_release_version(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы в начале и конце
    release_id = release_id.strip()
    
    domain, token = get_jira_domain_and_token(release_id)
    url = f"{domain}/rest/api/2/issue/{release_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, verify=False)
        response.raise_for_status()
        data = response.json()
        
        # Сначала пробуем получить версию из структурированных полей дистрибутива.
        # Для CLM/Delta версия может лежать как в customfield_21710, так и в customfield_27011,
        # иногда внутри вложенного объекта или текстового value/url.
        fields = data.get("fields", {})
        for field_id in ("customfield_21710", "customfield_27011"):
            version = _extract_version_from_distributive_field(fields.get(field_id, []))
            if version:
                logging.info(f"Версия релиза {release_id} найдена в {field_id}: {version}")
                return version
        
        # Fallback на customfield_21713
        customfield_21713 = fields.get("customfield_21713", "")
        if customfield_21713:
            match = re.search(r'[DP]-\d+\.\d+\.\d+-\d+', customfield_21713)
            if match:
                version = match.group(0)
                logging.info(f"Версия релиза {release_id} найдена в customfield_21713: {version}")
                return version
        
        # Fallback на полный текст ответа
        text = response.text
        match = re.search(r'[DP]-\d+\.\d+\.\d+-\d+', text)
        if match:
            version = match.group(0)
            logging.info(f"Версия релиза {release_id} найдена в тексте ответа: {version}")
            return version
        
        logging.warning(f"Версия релиза {release_id} не найдена ни в одном источнике")
        return ""
        
    except Exception as e:
        logging.error(f"Ошибка получения версии релиза: {e}")
        return ""

def get_issues_from_jira(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы
    release_id = release_id.strip()
    
    domain, token = get_jira_domain_and_token(release_id)
    url = f"{domain}/rest/api/2/issue/{release_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, verify=False)
        response.raise_for_status()
        data = response.json()
        issues = []
        issuelinks = data.get("fields", {}).get("issuelinks", [])
        for link in issuelinks:
            issue = link.get("inwardIssue")
            if issue:
                issue_type_name = issue.get("fields", {}).get("issuetype", {}).get("name", "")
                if issue_type_name not in ("Bug", "Story"):
                    continue
                key = issue.get("key")
                summary = issue.get("fields", {}).get("summary", "")
                issue_type = "Bug" if issue_type_name == "Bug" else "Story"
                issues.append({"key": key, "summary": summary, "type": issue_type})
        return issues
    except Exception as e:
        logging.error(f"Ошибка получения задач: {e}")
        return []

def get_ke_from_release(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы
    release_id = release_id.strip()
    
    domain, token = get_jira_domain_and_token(release_id)
    url = f"{domain}/rest/api/2/issue/{release_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, verify=False)
        response.raise_for_status()
        data = response.json()
        fields = data.get("fields", {})
        for field_id in ("customfield_21710", "customfield_27011"):
            ke = _extract_ke_from_distributive_field(fields.get(field_id, []))
            if ke:
                logging.info(f"КЭ релиза {release_id} найден в {field_id}: {ke}")
                return ke
        return ""
    except Exception as e:
        logging.error(f"Ошибка получения КЭ: {e}")
        return ""

def get_pob_from_release(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы
    release_id = release_id.strip()
    
    domain, token = get_jira_domain_and_token(release_id)
    url = f"{domain}/rest/api/2/issue/{release_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, verify=False)
        response.raise_for_status()
        data = response.json()
        issuelinks = data.get("fields", {}).get("issuelinks", [])
        pobs = []
        for link in issuelinks:
            if link.get("type", {}).get("id") == "11500" and "inwardIssue" in link:
                key = link["inwardIssue"].get("key")
                if key:
                    match = re.search(r'(?:EMRM|SMECLM)-(\d+)', key)
                    if match:
                        pobs.append((int(match.group(1)), key))
        if pobs:
            pobs.sort(reverse=True)
            return pobs[0][1]
        
        pobs = []
        for link in issuelinks:
            if link.get("type", {}).get("id") == "11600" and "inwardIssue" in link:
                key = link["inwardIssue"].get("key")
                if key:
                    # ИЗМЕНЕНО: Добавлена поддержка SMEPG в дополнение к SMECSC
                    match = re.search(r'(?:SMECSC|SMEPG)-(\d+)', key)
                    if match:
                        pobs.append((int(match.group(1)), key))
        if pobs:
            pobs.sort(reverse=True)
            return pobs[0][1]
        return ""
    except Exception as e:
        logging.error(f"Ошибка получения POB: {e}")
        return ""

def extract_sm_id_and_summary(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы
    release_id = release_id.strip()
    
    domain, token = get_jira_domain_and_token(release_id)
    url = f"{domain}/rest/api/2/issue/{release_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, verify=False)
        response.raise_for_status()
        data = response.json()
        fields = data.get("fields", {})
        summary = fields.get("summary", "")
        
        # ДОБАВЛЕНО: Сначала пробуем извлечь smId из customfield_27011 (новый формат для SMEPG/SMECSC)
        # через поле PARENT_CI (формат CI00356132 -> 356132)
        customfield_27011 = fields.get("customfield_27011", [])
        if customfield_27011 and isinstance(customfield_27011, list) and len(customfield_27011) > 0:
            for dist in customfield_27011:
                parent_ci = dist.get("PARENT_CI")
                if parent_ci and parent_ci.startswith("CI"):
                    # Извлекаем числовую часть из PARENT_CI (формат CI00356132 -> 356132)
                    sm_id = parent_ci[2:].lstrip('0')
                    if sm_id:
                        logging.info(f"Извлечен smId: {sm_id} из PARENT_CI {parent_ci} в customfield_27011")
                        return sm_id, summary
        
        # Продолжаем поиск в старых полях, если в customfield_27011 не нашли
        for field in ["customfield_18300", "customfield_22200"]:
            value = fields.get(field, [])
            if value and isinstance(value, list) and len(value) > 0:
                sm_id = value[0].get("smId")
                if sm_id:
                    logging.info(f"Извлечен smId: {sm_id} и summary: {summary} из {field}")
                    return sm_id, summary
        logging.warning(f"smId не найден для {release_id}")
        return None, summary
    except Exception as e:
        logging.error(f"Ошибка извлечения smId и summary: {e}")
        return None, ""

# --- Функции для получения дистрибутивов ---
def get_issue_id(issue_key):
    # ИСПРАВЛЕНО: Очищаем пробелы
    issue_key = issue_key.strip()
    
    domain, token = get_jira_domain_and_token(issue_key)
    url = f"{domain}/rest/api/2/issue/{issue_key}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        data = response.json()
        issue_id = data.get("id")
        if issue_id:
            logging.info(f"Получен ID задачи: {issue_id} для {issue_key}")
            return issue_id
        else:
            logging.error("ID задачи не найден в ответе")
            return None
    except Exception as e:
        logging.error(f"Ошибка при получении ID задачи: {e}")
        return None

def get_selected_distributives(issue_key):
    # ИСПРАВЛЕНО: Очищаем пробелы
    issue_key = issue_key.strip()
    
    domain, token = get_jira_domain_and_token(issue_key)
    url = f"{domain}/rest/api/2/issue/{issue_key}?fields=customfield_22401"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        selected = data.get("fields", {}).get("customfield_22401", [])
        result = [item.get("value") for item in selected if isinstance(item, dict) and "value" in item]
        
        logging.info(f"Выбрано дистрибутивов: {len(result)} для {issue_key}")
        return result
    except Exception as e:
        logging.error(f"Ошибка при получении выбранных дистрибутивов: {e}")
        return []

def get_all_distributives(issue_key):
    import time
    # ИСПРАВЛЕНО: Очищаем пробелы
    issue_key = issue_key.strip()
    
    issue_id = get_issue_id(issue_key)
    if not issue_id:
        return []
    
    domain, token = get_jira_domain_and_token(issue_key)
    base_url = f"{domain}/rest/sbtjirahttpcustomfieldseed/1.0/option"
    headers = {"Authorization": f"Bearer {token}"}
    
    all_options = []
    page_number = 0
    row_limit = 100
    total_rows = None
    
    try:
        while True:
            params = {
                "pageNumber": page_number,
                "rowLimit": row_limit,
                "term": "",
                "issueId": issue_id,
                "_": int(time.time() * 1000)
            }
            
            response = requests.get(base_url, headers=headers, params=params, verify=False, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if total_rows is None:
                total_rows = data.get("totalRows", 0)
                logging.info(f"Общее количество строк: {total_rows}")
            
            options = data.get("options", [])
            for option in options:
                if "value" in option:
                    all_options.append(option["value"])
            
            logging.info(f"Страница {page_number}: получено {len(options)} опций, всего собрано: {len(all_options)}")
            
            if len(options) < row_limit or (page_number + 1) * row_limit >= total_rows:
                break
            
            page_number += 1
            time.sleep(0.5)
            
        logging.info(f"Всего доступных дистрибутивов: {len(all_options)}")
        return all_options
        
    except Exception as e:
        logging.error(f"Ошибка при получении всех опций: {e}")
        return all_options

def get_distributives_info(issue_key):
    import time
    # ИСПРАВЛЕНО: Очищаем пробелы
    issue_key = issue_key.strip()
    
    logging.info(f"\n{'='*50}")
    logging.info(f"Обработка задачи: {issue_key}")
    
    selected = get_selected_distributives(issue_key)
    all_options = get_all_distributives(issue_key)
    not_selected = list(set(all_options) - set(selected)) if all_options else []
    
    ke_ids = []
    for option in all_options:
        if " (" in option and ")" in option:
            try:
                ke_str = option.rsplit(" (", 1)[-1].rsplit(")", 1)[0]
                if ke_str.isdigit():
                    ke_ids.append(ke_str)
            except:
                pass
    
    result = {
        "issue": issue_key,
        "selected": selected,
        "all_options": all_options,
        "not_selected": not_selected,
        "ke_ids": ke_ids,
        "selected_count": len(selected),
        "total_count": len(all_options),
        "not_selected_count": len(not_selected),
        "ke_ids_count": len(ke_ids)
    }
    
    logging.info(f"Результат для {issue_key}:")
    logging.info(f"Выбрано: {len(selected)}")
    logging.info(f"Доступно всего: {len(all_options)}")
    logging.info(f"Можно выбрать еще: {len(not_selected)}")
    logging.info(f"Найдено KE IDs: {len(ke_ids)}")
    
    return result
