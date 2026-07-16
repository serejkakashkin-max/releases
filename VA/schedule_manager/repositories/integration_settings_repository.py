from pathlib import Path

from VA.schedule_manager.models.integration_settings import CalendarIntegrationSettings
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import INTEGRATION_SETTINGS_FILE


class IntegrationSettingsRepository:
    def __init__(self, data_file: Path = INTEGRATION_SETTINGS_FILE) -> None:
        self.store = JsonFileStore(data_file, "integration_settings")

    def load_calendar(self) -> CalendarIntegrationSettings:
        data = self.store.load()
        if data is None:
            return CalendarIntegrationSettings()
        return CalendarIntegrationSettings.from_dict(data.get("calendar", {}))

    def save_calendar(self, settings: CalendarIntegrationSettings) -> None:
        self.store.save({"calendar": settings.to_dict()})
