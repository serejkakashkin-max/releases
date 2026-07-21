import json
from typing import Any, Dict, List, Optional

import requests

from config import DASHBOARD_ASSIGNEES, TOKENS
from services.release_zni_employee_provider import (
    get_release_zni_adapter_readiness as _get_release_zni_adapter_readiness,
    get_release_zni_users as _get_release_zni_users,
)


JIRA_DOMAIN = "https://jira.delta.sbrf.ru"
OPLOT_PROJECT_KEY = "OPLOT"
OPLOT_TASK_ISSUE_TYPE_ID = "3"
IMPLEMENTATION_LABEL = "Внедрение"
RELEASE_MONITOR_JIRA_USERS = [
    name
    for name in DASHBOARD_ASSIGNEES
    if not name.startswith(("Сафронов ", "Андреев "))
]


def get_release_zni_users() -> List[str]:
    return _get_release_zni_users(RELEASE_MONITOR_JIRA_USERS)


def get_release_zni_adapter_readiness() -> Dict[str, Any]:
    return _get_release_zni_adapter_readiness(RELEASE_MONITOR_JIRA_USERS)


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKENS['delta_token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _safe_json_response(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _get_current_jira_user() -> Dict[str, Any]:
    response = requests.get(
        f"{JIRA_DOMAIN}/rest/api/2/myself",
        headers=_headers(),
        verify=False,
        timeout=60,
    )
    payload = _safe_json_response(response)
    if response.status_code >= 400:
        raise ValueError(f"Не удалось определить пользователя Jira: {payload}")
    if not isinstance(payload, dict):
        raise ValueError("Jira /myself вернула неожиданный ответ")
    return payload


def _split_short_name(value: str) -> Optional[Dict[str, str]]:
    parts = str(value or "").strip().split()
    if len(parts) < 2:
        return None
    initials = "".join(part[:1] for part in parts[1:] if part).upper().replace(".", "")
    return {
        "surname": parts[0].lower(),
        "initials": initials,
    }


def resolve_dashboard_user_name(short_or_full_name: str) -> str:
    """Maps release-table short names like 'Кашкин С.Н.' to Jira display names."""
    raw_name = str(short_or_full_name or "").strip()
    if not raw_name:
        return ""

    release_zni_users = get_release_zni_users()
    if raw_name in release_zni_users:
        return raw_name

    parsed = _split_short_name(raw_name)
    if not parsed:
        return raw_name

    surname = parsed["surname"]
    initials = parsed["initials"]
    matches: List[str] = []
    for full_name in release_zni_users:
        full_parts = str(full_name or "").strip().split()
        if not full_parts or full_parts[0].lower() != surname:
            continue
        full_initials = "".join(part[:1] for part in full_parts[1:] if part and part != "-").upper()
        if initials and full_initials.startswith(initials):
            matches.append(full_name)

    if len(matches) == 1:
        return matches[0]
    return raw_name


def _normalize_name(value: str) -> str:
    return " ".join(str(value or "").replace(" - СРБ", "").split()).lower()


def _select_jira_user_name(users: Any, expected_display_name: str) -> str:
    if not isinstance(users, list):
        return ""
    expected = _normalize_name(expected_display_name)
    expected_surname = expected.split()[0] if expected else ""

    for user in users:
        display_name = str(user.get("displayName") or "").strip()
        if _normalize_name(display_name) == expected:
            return str(user.get("name") or user.get("key") or "").strip()

    for user in users:
        display_name = _normalize_name(user.get("displayName"))
        if expected and (expected in display_name or display_name in expected):
            return str(user.get("name") or user.get("key") or "").strip()

    for user in users:
        display_name = _normalize_name(user.get("displayName"))
        if expected_surname and display_name.startswith(expected_surname):
            return str(user.get("name") or user.get("key") or "").strip()

    return ""


def _jira_user_search(path: str, params: Dict[str, Any]) -> Any:
    response = requests.get(
        f"{JIRA_DOMAIN}{path}",
        headers=_headers(),
        params=params,
        verify=False,
        timeout=60,
    )
    if response.status_code >= 400:
        return []
    return _safe_json_response(response)


def _find_jira_user_name(display_or_login: str, *, assignable: bool) -> str:
    expected_name = str(display_or_login or "").strip()
    if not expected_name:
        return ""

    search_terms = [expected_name]
    surname = expected_name.split()[0] if expected_name.split() else ""
    if surname and surname not in search_terms:
        search_terms.append(surname)

    for term in search_terms:
        if assignable:
            users = _jira_user_search(
                "/rest/api/2/user/assignable/search",
                {"project": OPLOT_PROJECT_KEY, "query": term, "maxResults": 50},
            )
            user_name = _select_jira_user_name(users, expected_name)
            if user_name:
                return user_name

        users = _jira_user_search(
            "/rest/api/2/user/search",
            {"username": term, "maxResults": 50},
        )
        user_name = _select_jira_user_name(users, expected_name)
        if user_name:
            return user_name

    return ""


def _format_link_line(label: str, title: str, url: str) -> str:
    title = str(title or "").strip()
    url = str(url or "").strip()
    if title and url:
        return f"{label}: [{title}|{url}]"
    if title:
        return f"{label}: {title}"
    if url:
        return f"{label}: {url}"
    return f"{label}: не указано"


def _build_issue_summary(item: Dict[str, Any]) -> str:
    lines = item.get("release_name_lines") if isinstance(item.get("release_name_lines"), list) else []
    release_name = ""
    if len(lines) > 1:
        release_name = str(lines[1] or "").strip()
    elif lines:
        release_name = str(lines[0] or "").strip()
    if not release_name:
        release_name = str(item.get("release_summary") or item.get("release_key") or "Релиз").strip()

    release_version = str(item.get("release_version") or "").strip()
    if release_version:
        return f"{release_name}: {release_version}"
    return release_name


def _build_issue_description(item: Dict[str, Any]) -> str:
    release_key = str(item.get("release_key") or "").strip()
    rov_key = str(item.get("rov_key") or "").strip()
    return "\n".join([
        _format_link_line("Релиз", release_key, str(item.get("release_url") or "")),
        _format_link_line("РОВ", rov_key, str(item.get("rov_url") or "")),
        _format_link_line("Дистрибутив", str(item.get("release_version") or ""), str(item.get("release_dist_url") or "")),
    ])


def create_oplot_release_issue(item: Dict[str, Any], reporter_name: str = "") -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("Не переданы данные строки релиза")

    duty_short_name = str(item.get("psi_owner") or "").strip()
    responsibles = [
        str(value or "").strip()
        for value in (item.get("psi_responsibles") or [])
        if str(value or "").strip()
    ]
    if not duty_short_name:
        raise ValueError("Перед созданием ЗНИ заполните дежурного ОПЛОТ")
    if not responsibles:
        raise ValueError("Перед созданием ЗНИ заполните ответственного")

    assignee_display_name = resolve_dashboard_user_name(duty_short_name)
    selected_reporter = str(reporter_name or "").strip()
    if not selected_reporter:
        if len(responsibles) == 1:
            selected_reporter = responsibles[0]
        else:
            raise ValueError("Для нескольких ответственных нужно выбрать автора задачи")

    reporter_display_name = resolve_dashboard_user_name(selected_reporter)
    current_user = _get_current_jira_user()
    fallback_reporter_name = current_user.get("name") or current_user.get("key")
    assignee_jira_name = _find_jira_user_name(assignee_display_name, assignable=True)
    reporter_jira_name = _find_jira_user_name(reporter_display_name, assignable=False) if reporter_display_name else ""
    if not assignee_jira_name:
        raise ValueError(f"Не удалось найти исполнителя в Jira: {assignee_display_name}")

    fields = {
        "project": {"key": OPLOT_PROJECT_KEY},
        "issuetype": {"id": OPLOT_TASK_ISSUE_TYPE_ID},
        "summary": _build_issue_summary(item),
        "reporter": {"name": reporter_jira_name or fallback_reporter_name},
        "assignee": {"name": assignee_jira_name},
        "description": _build_issue_description(item),
        "labels": [IMPLEMENTATION_LABEL],
    }

    response = requests.post(
        f"{JIRA_DOMAIN}/rest/api/2/issue",
        headers=_headers(),
        data=json.dumps({"fields": fields}, ensure_ascii=False).encode("utf-8"),
        verify=False,
        timeout=60,
    )
    payload = _safe_json_response(response)
    if response.status_code >= 400:
        if reporter_jira_name and fallback_reporter_name and reporter_jira_name != fallback_reporter_name:
            fields["reporter"] = {"name": fallback_reporter_name}
            response = requests.post(
                f"{JIRA_DOMAIN}/rest/api/2/issue",
                headers=_headers(),
                data=json.dumps({"fields": fields}, ensure_ascii=False).encode("utf-8"),
                verify=False,
                timeout=60,
            )
            payload = _safe_json_response(response)

    if response.status_code >= 400:
        raise ValueError(f"Jira не создала ЗНИ: {payload}")

    issue_key = payload.get("key") if isinstance(payload, dict) else ""
    if not issue_key:
        raise ValueError(f"Jira создала задачу, но не вернула ключ: {payload}")

    return {
        "key": issue_key,
        "url": f"{JIRA_DOMAIN}/browse/{issue_key}",
        "summary": fields["summary"],
        "assignee": assignee_display_name,
        "reporter": fields["reporter"].get("name", ""),
        "response": payload,
    }
