from pathlib import Path
from typing import Optional

from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import SCHEDULE_DATA_FILE


class ScheduleRepository:
    def __init__(self, data_file: Path = SCHEDULE_DATA_FILE) -> None:
        self.data_file = data_file
        self.store = JsonFileStore(data_file, "schedule")

    def load(self) -> Optional[ScheduleSnapshot]:
        data = self.store.load()
        if data is None:
            return None

        return ScheduleSnapshot.from_dict(data)

    def save(self, snapshot: ScheduleSnapshot) -> None:
        self.store.save(snapshot.to_dict())

    def clear(self) -> None:
        self.store.clear()
