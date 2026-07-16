from pathlib import Path
from typing import Dict

from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import SCHEDULE_EDITS_FILE


class ScheduleEditRepository:
    def __init__(self, data_file: Path = SCHEDULE_EDITS_FILE) -> None:
        self.data_file = data_file
        self.store = JsonFileStore(data_file, "schedule_edits")

    def load_all(self) -> Dict[str, dict]:
        data = self.store.load()
        if data is None:
            return {}
        return data.get("edits", {})

    def load_month(self, workbook_id: str, sheet_name: str) -> Dict[str, Dict[str, str]]:
        return self.load_all().get(self._month_key(workbook_id, sheet_name), {})

    def save_cell(self, workbook_id: str, sheet_name: str, employee_name: str, day: int, shift_code: str) -> None:
        edits = self.load_all()
        month_key = self._month_key(workbook_id, sheet_name)
        month_edits = edits.setdefault(month_key, {})
        employee_edits = month_edits.setdefault(employee_name, {})
        employee_edits[str(day)] = shift_code

        self.store.save({"edits": edits})

    def _month_key(self, workbook_id: str, sheet_name: str) -> str:
        return f"{workbook_id}::{sheet_name}"
