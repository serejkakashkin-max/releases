from pathlib import Path
from typing import Optional

from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import SCHEDULE_DATA_FILE


class ScheduleRepository:
    def __init__(self, data_file: Path = SCHEDULE_DATA_FILE) -> None:
        self.data_file = data_file
        self.store = JsonFileStore(data_file, "schedule", backup_limit=5)

    def load(self) -> Optional[ScheduleSnapshot]:
        data = self.store.load()
        if data is None:
            return None
        snapshot = ScheduleSnapshot.from_dict(data)
        from VA.schedule_manager.integrations.employee_directory_adapter import (
            project_schedule_snapshot_to_current_directory,
        )

        return project_schedule_snapshot_to_current_directory(snapshot)

    def save(self, snapshot: ScheduleSnapshot) -> None:
        self.store.save(snapshot.to_dict())

    def clear(self) -> None:
        self.store.clear()
