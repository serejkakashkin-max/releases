from pathlib import Path
from typing import List

from VA.schedule_manager.models.shift import ShiftDefinition
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import SHIFTS_DATA_FILE


class ShiftRepository:
    def __init__(self, data_file: Path = SHIFTS_DATA_FILE) -> None:
        self.data_file = data_file
        self.store = JsonFileStore(data_file, "shifts")

    def load_all(self) -> List[ShiftDefinition]:
        data = self.store.load()
        if data is None:
            return []

        return [ShiftDefinition.from_dict(item) for item in data.get("shifts", [])]

    def save_all(self, shifts: List[ShiftDefinition]) -> None:
        self.store.save({"shifts": [shift.to_dict() for shift in shifts]})
