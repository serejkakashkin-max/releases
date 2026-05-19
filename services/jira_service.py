import re
import logging
import requests
import time
import threading
from datetime import datetime, timedelta
from config import TOKENS


VERSION_PATTERN = re.compile(r"[DP]-\d+(?:\.\d+){2}(?:-[A-Za-z0-9_]+)+")
RELEASE_SNAPSHOT_TTL_SECONDS = 120
RELEASE_SNAPSHOT_FIELDS = ",".join([
    "summary",
    "issuelinks",
    "customfield_21710",
    "customfield_27011",
    "customfield_21713",
    "customfield_18300",
    "customfield_22200",
])
_RELEASE_SNAPSHOT_CACHE = {}
_RELEASE_SNAPSHOT_LOCK = threading.RLock()


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
    for value in _iter_nested_values(raw_value):
        if isinstance(value, dict):
            for key in ("version", "buildVersion", "release_version", "releases_version", "value", "url"):
                raw_version = value.get(key)
                if not raw_version:
                    continue

                match = VERSION_PATTERN.search(str(raw_version))
                if match:
                    return match.group(0)
        else:
            match = VERSION_PATTERN.search(str(value))
            if match:
                return match.group(0)

    return ""


def get_jira_domain_and_token(release_id):
    # ИЗМЕНЕНО: релизы из delta-домена
    delta_prefixes = ("SMECSC", "SMEPG", "HELPERAI", "AIGAS")
    if any(release_id.startswith(prefix) for prefix in delta_prefixes):
        return "https://jira.delta.sbrf.ru", TOKENS["delta_token"]
    return "https://jira.sberbank.ru", TOKENS["sberbank_token"]


def _extract_release_version_from_fields(fields):
    for field_id in ("customfield_21710", "customfield_27011"):
        version = _extract_version_from_distributive_field(fields.get(field_id, []))
        if version:
            return version

    customfield_21713 = fields.get("customfield_21713", "")
    if customfield_21713:
        match = VERSION_PATTERN.search(str(customfield_21713))
        if match:
            return match.group(0)
    return ""


def _extract_distribution_ke_from_fields(fields):
    for field_id in ("customfield_21710", "customfield_27011"):
        ke = _extract_ke_from_distributive_field(fields.get(field_id, []))
        if ke:
            return ke
    return ""


def _extract_template_sm_id_from_fields(fields):
    customfield_27011 = fields.get("customfield_27011", [])
    if customfield_27011 and isinstance(customfield_27011, list):
        for dist in customfield_27011:
            if not isinstance(dist, dict):
                continue
            parent_ci = dist.get("PARENT_CI")
            if parent_ci and str(parent_ci).startswith("CI"):
                sm_id = str(parent_ci)[2:].lstrip("0")
                if sm_id:
                    return sm_id

    for field in ["customfield_18300", "customfield_22200"]:
        value = fields.get(field, [])
        if value and isinstance(value, list):
            first_value = value[0] if value else {}
            if isinstance(first_value, dict):
                sm_id = first_value.get("smId")
                if sm_id:
                    return sm_id
    return None


def _extract_related_issues_from_fields(fields):
    issues = []
    for link in fields.get("issuelinks", []) or []:
        issue = link.get("inwardIssue")
        if not issue:
            continue
        issue_type_name = issue.get("fields", {}).get("issuetype", {}).get("name", "")
        if issue_type_name not in ("Bug", "Story"):
            continue
        key = issue.get("key")
        summary = issue.get("fields", {}).get("summary", "")
        issue_type = "Bug" if issue_type_name == "Bug" else "Story"
        issues.append({"key": key, "summary": summary, "type": issue_type})
    return issues


def _extract_pob_from_fields(fields):
    issuelinks = fields.get("issuelinks", []) or []
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
                match = re.search(r'(?:SMECSC|SMEPG)-(\d+)', key)
                if match:
                    pobs.append((int(match.group(1)), key))
    if pobs:
        pobs.sort(reverse=True)
        return pobs[0][1]
    return ""


def _build_release_snapshot(release_id, domain, issue_data):
    fields = issue_data.get("fields", {}) if isinstance(issue_data, dict) else {}
    summary = fields.get("summary", "")
    return {
        "release_id": release_id,
        "domain": domain,
        "summary": summary,
        "template_sm_id": _extract_template_sm_id_from_fields(fields),
        "release_version": _extract_release_version_from_fields(fields),
        "ke": _extract_distribution_ke_from_fields(fields),
        "pob": _extract_pob_from_fields(fields),
        "issues": _extract_related_issues_from_fields(fields),
        "fields": fields,
        "fetched_at": time.time(),
    }


def get_release_jira_snapshot(release_id, force_refresh=False):
    release_id = (release_id or "").strip().upper()
    if not release_id:
        return {
            "release_id": "",
            "domain": "",
            "summary": "",
            "template_sm_id": None,
            "release_version": "",
            "ke": "",
            "pob": "",
            "issues": [],
            "fields": {},
            "fetched_at": time.time(),
            "error": "No release_id provided",
        }

    now = time.time()
    with _RELEASE_SNAPSHOT_LOCK:
        cached = _RELEASE_SNAPSHOT_CACHE.get(release_id)
        if (
            not force_refresh
            and cached
            and (now - float(cached.get("fetched_at") or 0)) < RELEASE_SNAPSHOT_TTL_SECONDS
        ):
            snapshot = dict(cached)
            snapshot["from_cache"] = True
            return snapshot

    started = time.perf_counter()
    domain, token = get_jira_domain_and_token(release_id)
    url = f"{domain}/rest/api/2/issue/{release_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(
            url,
            headers=headers,
            params={"fields": RELEASE_SNAPSHOT_FIELDS},
            verify=False,
            timeout=30,
        )
        response.raise_for_status()
        snapshot = _build_release_snapshot(release_id, domain, response.json())
        snapshot["from_cache"] = False
        with _RELEASE_SNAPSHOT_LOCK:
            _RELEASE_SNAPSHOT_CACHE[release_id] = dict(snapshot)
        logging.debug(
            "Jira release snapshot loaded for %s in %.1f ms",
            release_id,
            (time.perf_counter() - started) * 1000,
        )
        return snapshot
    except Exception as e:
        logging.error(f"Ошибка получения снимка релиза {release_id}: {e}")
        return {
            "release_id": release_id,
            "domain": domain,
            "summary": "",
            "template_sm_id": None,
            "release_version": "",
            "ke": "",
            "pob": "",
            "issues": [],
            "fields": {},
            "fetched_at": time.time(),
            "from_cache": False,
            "error": str(e),
        }

def get_release_version(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы в начале и конце
    release_id = release_id.strip()
    snapshot = get_release_jira_snapshot(release_id)
    version = snapshot.get("release_version") or ""
    if version:
        logging.info(f"Версия релиза {release_id} найдена: {version}")
        return version
    logging.warning(f"Версия релиза {release_id} не найдена ни в одном источнике")
    return ""


def get_issues_from_jira(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы
    release_id = release_id.strip()
    return list(get_release_jira_snapshot(release_id).get("issues") or [])

def get_ke_from_release(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы
    release_id = release_id.strip()
    ke = get_release_jira_snapshot(release_id).get("ke") or ""
    if ke:
        logging.info(f"КЭ релиза {release_id} найден: {ke}")
    return ke

def get_pob_from_release(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы
    release_id = release_id.strip()
    return get_release_jira_snapshot(release_id).get("pob") or ""

def extract_sm_id_and_summary(release_id):
    # ИСПРАВЛЕНО: Очищаем пробелы
    release_id = release_id.strip()
    snapshot = get_release_jira_snapshot(release_id)
    sm_id = snapshot.get("template_sm_id")
    summary = snapshot.get("summary") or ""
    if sm_id:
        logging.info(f"Извлечен smId: {sm_id} и summary: {summary}")
        return sm_id, summary
    logging.warning(f"smId не найден для {release_id}")
    return None, summary

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
