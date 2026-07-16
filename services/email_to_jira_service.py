"""Jira task creation for email automation routes."""

from __future__ import annotations

import json
from typing import Any, Dict

import requests

from config import TOKENS
from services.feature_flags_service import get_automation_config, get_jira_domain_config


class EmailToJiraError(RuntimeError):
    pass


def _connection(domain_key: str) -> Dict[str, str]:
    config = get_jira_domain_config(domain_key)
    if not config:
        raise EmailToJiraError(f"Unknown Jira domain: {domain_key}")
    token = str(TOKENS.get(config.get("token_key"), "") or "").strip()
    if not token:
        raise EmailToJiraError(f"Jira token is not configured for domain: {domain_key}")
    return {"url": str(config["url"]).rstrip("/"), "token": token}


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:1000]


def _find_reporter(connection: Dict[str, str], email: str) -> str:
    email = str(email or "").strip().lower()
    if not email:
        return ""
    responsible = get_automation_config("release_monitor_responsible_email")
    employees = responsible.get("employee_recipients") if isinstance(responsible, dict) else {}
    known_emails = set()
    if isinstance(employees, dict):
        for value in employees.values():
            if isinstance(value, dict) and bool(value.get("enabled", True)):
                known_emails.update(str(item).strip().lower() for item in value.get("emails") or [])
            elif isinstance(value, list):
                known_emails.update(str(item).strip().lower() for item in value)
    elif isinstance(employees, list):
        for employee in employees:
            if isinstance(employee, dict):
                known_emails.update(str(item).strip().lower() for item in employee.get("emails") or [])
    if known_emails and email not in known_emails:
        return ""
    response = requests.get(
        f"{connection['url']}/rest/api/2/user/search",
        headers=_headers(connection["token"]),
        params={"username": email, "maxResults": 50},
        verify=False,
        timeout=30,
    )
    if response.status_code >= 400:
        return ""
    payload = _json(response)
    if not isinstance(payload, list):
        return ""
    exact = [
        user for user in payload
        if str(user.get("emailAddress") or "").strip().lower() == email
    ]
    if len(exact) != 1:
        return ""
    return str(exact[0].get("name") or exact[0].get("key") or "").strip()


def _current_user(connection: Dict[str, str]) -> str:
    response = requests.get(
        f"{connection['url']}/rest/api/2/myself",
        headers=_headers(connection["token"]),
        verify=False,
        timeout=30,
    )
    if response.status_code >= 400:
        return ""
    payload = _json(response)
    return str(payload.get("name") or payload.get("key") or "").strip() if isinstance(payload, dict) else ""


def create_email_jira_task(event: Dict[str, Any]) -> Dict[str, Any]:
    route = event.get("route") if isinstance(event.get("route"), dict) else {}
    domain = str(route.get("jira_domain") or "sberbank").strip().lower()
    project = str(event.get("space") or "").strip()
    if not project:
        raise EmailToJiraError("Jira project is not configured for the email route")
    connection = _connection(domain)
    mail = event.get("mail") if isinstance(event.get("mail"), dict) else {}
    from_rows = mail.get("from") if isinstance(mail.get("from"), list) else []
    sender_email = str((from_rows[0] if from_rows else {}).get("email") or "").strip()
    reporter = _find_reporter(connection, sender_email) or _current_user(connection)

    description = str(mail.get("body") or "")
    description += (
        f"\n\nJira domain: {domain}\n"
        f"Jira project: {project}"
    )
    subject = str(mail.get("subject") or event.get("summary") or "Email task").strip()
    issue_type = str(route.get("jira_issue_type") or "Story").strip() or "Story"
    issue_type_id = str(route.get("jira_issue_type_id") or "").strip()
    epic_name_field = str(route.get("jira_epic_name_field") or "").strip()
    fields: Dict[str, Any] = {
        "project": {"key": project},
        "issuetype": ({"id": issue_type_id} if issue_type_id else {"name": issue_type}),
        "priority": {"name": str(route.get("jira_priority") or "Minor")},
        "summary": subject[:220],
        "description": description[:12000],
        "labels": list(route.get("jira_labels") or []),
    }
    if reporter:
        fields["reporter"] = {"name": reporter}
    team = route.get("jira_team") if isinstance(route.get("jira_team"), dict) else {}
    field_id = str(team.get("field_id") or "").strip()
    value_id = str(team.get("value_id") or "").strip()
    team_name = str(team.get("name") or "").strip()
    if project.upper() == "EMRM" and issue_type.lower() == "epic":
        field_id = field_id or "customfield_11902"
        value_id = value_id or "6651"
        team_name = "[Фокус] ForREST"
    if field_id and team_name:
        fields[field_id] = team_name
    elif field_id and value_id:
        fields[field_id] = value_id
    if issue_type.lower() == "epic":
        epic_name_field = epic_name_field or "customfield_10007"
        fields[epic_name_field] = subject[:255]
    epic_link = route.get("jira_epic_link") if isinstance(route.get("jira_epic_link"), dict) else {}
    epic_link_field = str(epic_link.get("field_id") or "").strip()
    epic_link_key = str(epic_link.get("key") or "").strip()
    if epic_link_field and epic_link_key:
        fields[epic_link_field] = epic_link_key

    response = requests.post(
        f"{connection['url']}/rest/api/2/issue",
        headers=_headers(connection["token"]),
        data=json.dumps({"fields": fields}, ensure_ascii=False).encode("utf-8"),
        verify=False,
        timeout=60,
    )
    payload = _json(response)
    if response.status_code >= 400:
        raise EmailToJiraError(f"Jira returned HTTP {response.status_code}: {payload}")
    issue_key = str(payload.get("key") or "").strip() if isinstance(payload, dict) else ""
    if not issue_key:
        raise EmailToJiraError("Jira created a task without returning an issue key")
    return {
        "created_at": event.get("created_at") or "",
        "task_key": issue_key,
        "task_url": f"{connection['url']}/browse/{issue_key}",
        "domain": domain,
        "project": project,
        "response": payload,
    }
