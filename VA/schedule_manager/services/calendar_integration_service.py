import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Set
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from VA.schedule_manager.models.integration_settings import CalendarIntegrationSettings
from VA.schedule_manager.repositories.integration_settings_repository import IntegrationSettingsRepository


CONSULTANT_DEFAULT_URL = "https://www.consultant.ru/law/ref/calendar/proizvodstvennye/"
ISDAYOFF_DEFAULT_URL = "https://isdayoff.ru/api/getdata"
MAX_CALENDAR_RESPONSE_BYTES = 512 * 1024
MONTH_NAME_TO_NUMBER = {
    "Январь": 1,
    "Февраль": 2,
    "Март": 3,
    "Апрель": 4,
    "Май": 5,
    "Июнь": 6,
    "Июль": 7,
    "Август": 8,
    "Сентябрь": 9,
    "Октябрь": 10,
    "Ноябрь": 11,
    "Декабрь": 12,
}


class CalendarIntegrationError(Exception):
    pass


@dataclass(frozen=True)
class ProductionCalendar:
    holidays: Set[date]
    workdays: Set[date]
    source: str
    warning: str = ""


class CalendarIntegrationService:
    def __init__(self, repository: IntegrationSettingsRepository) -> None:
        self.repository = repository

    def get_settings(self) -> CalendarIntegrationSettings:
        return self.repository.load_calendar()

    def save_settings(self, form: dict) -> CalendarIntegrationSettings:
        provider = str(form.get("provider", "consultant")).strip() or "consultant"
        api_url = str(form.get("api_url", "")).strip()
        if provider == "consultant" and not api_url:
            api_url = CONSULTANT_DEFAULT_URL
        if provider == "isdayoff" and not api_url:
            api_url = ISDAYOFF_DEFAULT_URL
        settings = CalendarIntegrationSettings(
            enabled=form.get("enabled") == "on",
            provider=provider,
            api_url=api_url,
            api_token=str(form.get("api_token", "")).strip(),
            github_config_url=str(form.get("github_config_url", "")).strip(),
            github_branch=str(form.get("github_branch", "")).strip(),
            timeout_seconds=self._positive_int(form.get("timeout_seconds", 5), 5),
        )
        self.repository.save_calendar(settings)
        return settings

    def load_calendar(self, year: int, month: int) -> ProductionCalendar:
        settings = self.repository.load_calendar()
        if not settings.enabled:
            return ProductionCalendar(set(), set(), "Не настроен", "Производственный календарь не настроен.")
        if not settings.api_url:
            return ProductionCalendar(set(), set(), settings.provider, "Не указан адрес API производственного календаря.")

        if settings.provider == "consultant":
            try:
                return self._load_consultant_calendar(settings, year, month)
            except CalendarIntegrationError as exc:
                return ProductionCalendar(set(), set(), settings.provider, str(exc))

        if settings.provider == "isdayoff":
            try:
                return self._load_isdayoff_calendar(settings, year, month)
            except CalendarIntegrationError as exc:
                return ProductionCalendar(set(), set(), settings.provider, str(exc))

        try:
            payload = self._request_json_calendar(settings, year, month)
        except CalendarIntegrationError as exc:
            return ProductionCalendar(set(), set(), settings.provider, str(exc))

        return ProductionCalendar(
            holidays=self._parse_dates(payload.get("holidays", []), year, month),
            workdays=self._parse_dates(payload.get("workdays", []), year, month),
            source=settings.provider,
        )

    def _load_consultant_calendar(self, settings: CalendarIntegrationSettings, year: int, month: int) -> ProductionCalendar:
        raw = self._request_text_url(settings, self._consultant_year_url(settings.api_url, year))
        holidays, workdays = self._parse_consultant_html(raw, year, month)
        if not holidays and not workdays:
            raise CalendarIntegrationError("Не удалось найти выбранный месяц на странице КонсультантПлюс. Новый график создан без праздничных отметок.")
        return ProductionCalendar(holidays=holidays, workdays=workdays, source="КонсультантПлюс")

    def _load_isdayoff_calendar(self, settings: CalendarIntegrationSettings, year: int, month: int) -> ProductionCalendar:
        raw = self._request_text_calendar(
            settings,
            {
                "year": year,
                "month": month,
                "cc": "ru",
                "pre": 1,
                "holiday": 1,
            },
        )
        values = raw.strip()
        if not values:
            raise CalendarIntegrationError("Производственный календарь вернул пустой ответ. Новый график создан без праздничных отметок.")

        holidays = set()
        workdays = set()
        for index, code in enumerate(values, start=1):
            try:
                current = date(year, month, index)
            except ValueError:
                continue
            if code == "8":
                holidays.add(current)
            elif code in {"0", "2", "4"}:
                workdays.add(current)

        return ProductionCalendar(holidays=holidays, workdays=workdays, source="isDayOff")

    def _consultant_year_url(self, api_url: str, year: int) -> str:
        if "{year}" in api_url:
            return api_url.format(year=year)
        return f"{api_url.rstrip('/')}/{year}/"

    def _parse_consultant_html(self, raw: str, year: int, target_month: int) -> tuple[Set[date], Set[date]]:
        holidays: Set[date] = set()
        workdays: Set[date] = set()
        for table_match in re.finditer(r"<table\s+class=\"cal\".*?</table>", raw, flags=re.DOTALL):
            table = table_match.group(0)
            month_match = re.search(r"<th[^>]*class=\"month\"[^>]*>\s*([^<]+?)\s*</th>", table)
            if not month_match:
                continue
            month = MONTH_NAME_TO_NUMBER.get(month_match.group(1).strip())
            if month != target_month:
                continue

            for cell_match in re.finditer(r"<td\s+class=\"([^\"]*)\"[^>]*>(.*?)</td>", table, flags=re.DOTALL):
                classes = set(cell_match.group(1).split())
                if "inactively" in classes:
                    continue
                day_match = re.search(r"\d+", re.sub(r"<[^>]+>", "", cell_match.group(2)))
                if not day_match:
                    continue
                current = date(year, month, int(day_match.group(0)))
                if "holiday" in classes:
                    holidays.add(current)
                elif "weekend" not in classes:
                    workdays.add(current)
            break
        return holidays, workdays

    def _request_text_url(self, settings: CalendarIntegrationSettings, url: str) -> str:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "schedule-manager/1.0",
        }
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=settings.timeout_seconds) as response:
                return self._read_limited_text(response)
        except (OSError, URLError) as exc:
            raise CalendarIntegrationError("Страница производственного календаря КонсультантПлюс сейчас недоступна. Новый график создан без праздничных отметок.") from exc

    def _request_json_calendar(self, settings: CalendarIntegrationSettings, year: int, month: int) -> dict:
        raw = self._request_text_calendar(settings, {"year": year, "month": month})
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CalendarIntegrationError("Производственный календарь вернул не JSON. Новый график создан без праздничных отметок.") from exc
        if not isinstance(payload, dict):
            raise CalendarIntegrationError("Производственный календарь вернул неожиданный формат. Новый график создан без праздничных отметок.")
        return payload

    def _request_text_calendar(self, settings: CalendarIntegrationSettings, params: dict) -> str:
        separator = "&" if "?" in settings.api_url else "?"
        url = f"{settings.api_url}{separator}{urlencode(params)}"
        headers = {"Accept": "application/json, text/plain, */*"}
        if settings.api_token:
            headers["Authorization"] = f"Bearer {settings.api_token}"

        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=settings.timeout_seconds) as response:
                return self._read_limited_text(response)
        except (OSError, URLError) as exc:
            raise CalendarIntegrationError("Производственный календарь сейчас недоступен. Новый график создан без праздничных отметок.") from exc


    def _read_limited_text(self, response) -> str:
        raw = response.read(MAX_CALENDAR_RESPONSE_BYTES + 1)
        if len(raw) > MAX_CALENDAR_RESPONSE_BYTES:
            raise CalendarIntegrationError("Calendar response is too large.")
        return raw.decode("utf-8", errors="replace")

    def _parse_dates(self, values: object, year: int, month: int) -> Set[date]:
        if not isinstance(values, list):
            return set()

        parsed = set()
        for value in values:
            try:
                if isinstance(value, int):
                    parsed.add(date(year, month, value))
                else:
                    parsed.add(date.fromisoformat(str(value)))
            except ValueError:
                continue
        return parsed

    def _positive_int(self, value: object, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, min(number, 30))
