import html
import json
import logging
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import SCRIPT_DIR, TOKENS


CONFLUENCE_DELTA_BASE = "https://confluence.delta.sbrf.ru"
PSI_JENKINS_PAGE_ID = "17299800209"
PSI_JENKINS_CACHE_FILE = SCRIPT_DIR / "cache" / "psi_jenkins_instructions.json"
PSI_JENKINS_PARSER_VERSION = 2


def _now_text() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def _normalize_ke(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    match = re.search(r"(?:CI)?0*(\d{5,})", raw)
    if not match:
        return ""
    return match.group(1).lstrip("0") or match.group(1)


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


class _ConfluenceTextParser(HTMLParser):
    BLOCK_TAGS = {
        "p",
        "div",
        "tr",
        "td",
        "th",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
    }

    def __init__(self):
        super().__init__()
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs or {}).get("href")
            if href and href.startswith("http"):
                self.parts.append(f" {href} ")

    def handle_endtag(self, tag):
        if str(tag or "").lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def lines(self) -> List[str]:
        text = html.unescape("".join(self.parts))
        return [
            _normalize_text(line)
            for line in text.splitlines()
            if _normalize_text(line)
        ]


def _storage_html_to_lines(storage_html: str) -> List[str]:
    parser = _ConfluenceTextParser()
    parser.feed(storage_html or "")
    return parser.lines()


def _extract_ke_from_line(line: str) -> str:
    normalized_line = str(line or "")
    parenthesized = re.findall(r"\(([^()]*)\)", normalized_line)
    for value in reversed(parenthesized):
        ke = _normalize_ke(value)
        if ke:
            return ke
    return ""


def _extract_contour(*values: str) -> str:
    text = " ".join(str(value or "") for value in values).upper()
    if "GREEN" in text or "PSI-GR" in text or "PSI_GR" in text:
        return "GREEN"
    if "BLUE" in text or "PSI-BL" in text or "PSI_BL" in text:
        return "BLUE"
    return ""


def _looks_like_section(line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "config_dir", "subsystem")):
        return False
    if ":" in lowered:
        return False
    if any(marker in lowered for marker in ("hosts_group", "host_group", "hosts_to_update", "ansible", "inventory")):
        return False
    if _extract_ke_from_line(value):
        return False
    return len(value) <= 80


def parse_psi_jenkins_instructions(storage_html: str) -> List[Dict[str, Any]]:
    lines = _storage_html_to_lines(storage_html)
    instructions: List[Dict[str, Any]] = []
    current_section = ""
    index = 0

    while index < len(lines):
        line = lines[index]
        ke_id = _extract_ke_from_line(line)

        if not ke_id:
            if _looks_like_section(line):
                current_section = line
            index += 1
            continue

        entry_lines = [line]
        index += 1
        while index < len(lines):
            next_line = lines[index]
            if _extract_ke_from_line(next_line):
                break
            if _looks_like_section(next_line) and len(entry_lines) >= 2:
                break
            entry_lines.append(next_line)
            index += 1

        raw_text = "\n".join(entry_lines)
        urls = re.findall(r"https?://\S+", raw_text)
        config_dir = ""
        subsystem = ""
        for entry_line in entry_lines:
            config_match = re.search(r"CONFIG_DIR\s*:\s*(.+)", entry_line, re.IGNORECASE)
            if config_match:
                config_dir = _normalize_text(config_match.group(1))
            subsystem_match = re.search(r"SUBSYSTEM\s*:\s*(.+)", entry_line, re.IGNORECASE)
            if subsystem_match:
                subsystem = _normalize_text(subsystem_match.group(1))

        instructions.append(
            {
                "section": current_section,
                "title": line,
                "ke_id": ke_id,
                "ke_label": f"CI{ke_id}",
                "contour": _extract_contour(line, raw_text),
                "jenkins_url": urls[0] if urls else "",
                "config_dir": config_dir,
                "subsystem": subsystem,
                "raw_lines": entry_lines,
            }
        )

    return instructions


def _load_cache() -> Dict[str, Any]:
    if not PSI_JENKINS_CACHE_FILE.exists():
        return {}
    try:
        payload = json.loads(PSI_JENKINS_CACHE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        logging.warning("PSI Jenkins: failed to load cache: %s", exc)
        return {}


def _save_cache(payload: Dict[str, Any]) -> None:
    try:
        PSI_JENKINS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PSI_JENKINS_CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("PSI Jenkins: failed to save cache: %s", exc)


def _confluence_headers() -> Dict[str, str]:
    token = str(TOKENS.get("confluence_delta_token", "") or "").strip()
    if not token:
        raise ValueError("Не настроен token доступа к Confluence")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _fetch_page(expand: str) -> Dict[str, Any]:
    url = f"{CONFLUENCE_DELTA_BASE}/rest/api/content/{PSI_JENKINS_PAGE_ID}"
    response = requests.get(
        url,
        headers=_confluence_headers(),
        params={"expand": expand},
        verify=False,
        timeout=60,
    )
    if not response.ok:
        raise ValueError(f"Confluence GET failed ({response.status_code}): {response.text[:300]}")
    return response.json()


def get_psi_jenkins_instruction_cache(force_refresh: bool = False) -> Dict[str, Any]:
    cache = _load_cache()
    cached_version = int(cache.get("page_version") or 0)

    try:
        meta = _fetch_page("version,title")
        page_version = int(((meta.get("version") or {}).get("number")) or 0)
        page_title = str(meta.get("title") or "").strip()

        if (
            not force_refresh
            and cache.get("page_id") == PSI_JENKINS_PAGE_ID
            and cached_version == page_version
            and int(cache.get("parser_version") or 0) == PSI_JENKINS_PARSER_VERSION
            and isinstance(cache.get("instructions"), list)
        ):
            return {
                **cache,
                "cache_status": "hit",
                "page_url": f"{CONFLUENCE_DELTA_BASE}/pages/viewpage.action?pageId={PSI_JENKINS_PAGE_ID}",
            }

        page = _fetch_page("body.storage,version,title")
        storage_html = (((page.get("body") or {}).get("storage") or {}).get("value") or "")
        instructions = parse_psi_jenkins_instructions(storage_html)
        payload = {
            "page_id": PSI_JENKINS_PAGE_ID,
            "page_title": str(page.get("title") or page_title).strip(),
            "page_version": int(((page.get("version") or {}).get("number")) or page_version),
            "parser_version": PSI_JENKINS_PARSER_VERSION,
            "updated_at": _now_text(),
            "instructions": instructions,
            "page_url": f"{CONFLUENCE_DELTA_BASE}/pages/viewpage.action?pageId={PSI_JENKINS_PAGE_ID}",
            "cache_status": "refreshed",
        }
        _save_cache(payload)
        return payload
    except Exception:
        if isinstance(cache.get("instructions"), list):
            return {
                **cache,
                "cache_status": "stale",
                "page_url": f"{CONFLUENCE_DELTA_BASE}/pages/viewpage.action?pageId={PSI_JENKINS_PAGE_ID}",
            }
        raise


def find_psi_jenkins_instructions_by_ke(ke_value: Any) -> Dict[str, Any]:
    ke_id = _normalize_ke(ke_value)
    if not ke_id:
        return {
            "ke_id": "",
            "matches": [],
            "cache": get_psi_jenkins_instruction_cache(),
        }

    cache = get_psi_jenkins_instruction_cache()
    matches = [
        item for item in cache.get("instructions", [])
        if _normalize_ke(item.get("ke_id")) == ke_id
    ]
    matches.sort(key=lambda item: (str(item.get("contour") or ""), str(item.get("title") or "")))
    return {
        "ke_id": ke_id,
        "matches": matches,
        "cache": cache,
    }
