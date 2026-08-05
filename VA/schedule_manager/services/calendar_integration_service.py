from dataclasses import dataclass
from datetime import date
from typing import Set

try:
    import holidays
    HOLIDAYS_AVAILABLE = True
except ImportError:
    HOLIDAYS_AVAILABLE = False

from VA.schedule_manager.models.integration_settings import CalendarIntegrationSettings
from VA.schedule_manager.repositories.integration_settings_repository import IntegrationSettingsRepository


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
        # Игнорируем всё, кроме enabled и timeout
        settings = CalendarIntegrationSettings(
            enabled=form.get("enabled") == "on",
            provider="holidays",
            api_url="",
            timeout_seconds=self._positive_int(form.get("timeout_seconds", 5), 5),
        )
        self.repository.save_calendar(settings)
        return settings

    def load_calendar(self, year: int, month: int) -> ProductionCalendar:
        settings = self.repository.load_calendar()
        if not settings.enabled:
            return ProductionCalendar(set(), set(), "Не настроен", "Производственный календарь не настроен.")

        return self._load_holidays_library(year)

    def _load_holidays_library(self, year: int) -> ProductionCalendar:
        """Загружает праздники из библиотеки holidays."""
        if not HOLIDAYS_AVAILABLE:
            return ProductionCalendar(
                set(),
                set(),
                "holidays library",
                "Библиотека holidays не установлена. Установите: pip install holidays"
            )

        try:
            ru_holidays = holidays.Russia(years=year)
            holidays_set = set()
            for holiday_date, name in ru_holidays.items():
                holidays_set.add(holiday_date)

            return ProductionCalendar(
                holidays=holidays_set,
                workdays=set(),
                source="holidays library",
                warning=""
            )
        except Exception as exc:
            return ProductionCalendar(
                set(),
                set(),
                "holidays library",
                f"Ошибка загрузки праздников: {exc}"
            )

    def _positive_int(self, value: object, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, min(number, 30))