from typing import Dict, Iterable, Optional, Set
from pathlib import Path

from VA.schedule_manager.models.schedule_grid import ScheduleGrid
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.config import UPLOAD_DIR


class ScheduleService:
    def __init__(self, repository: ScheduleRepository) -> None:
        self.repository = repository

    def get_current(self) -> Optional[ScheduleSnapshot]:
        return self.repository.load()

    def month_options(self) -> list:
        snapshot = self.get_current()
        if snapshot is None:
            return []
        return snapshot.month_options()

    def get_month_grid(self, sheet_name: str) -> ScheduleGrid:
        snapshot = self.get_current()
        if snapshot is None:
            raise KeyError(sheet_name)
        return snapshot.get_month_grid(sheet_name)

    def save_month_grid(
        self,
        sheet_name: str,
        grid: ScheduleGrid,
        *,
        clear_metadata_keys: Iterable[str] = (),
        metadata_updates: Optional[Dict[str, dict]] = None,
    ) -> Set[str]:
        snapshot = self.get_current()
        if snapshot is None:
            raise KeyError(sheet_name)
        updated = snapshot.replace_month_grid(sheet_name, grid)
        cleared = set()
        for key in clear_metadata_keys:
            if updated.get_month_metadata(sheet_name, key):
                updated = updated.clear_month_metadata(sheet_name, key)
                cleared.add(key)
        for key, value in (metadata_updates or {}).items():
            updated = updated.set_month_metadata(sheet_name, key, value)
        self.repository.save(updated)
        return cleared

    def get_month_metadata(self, sheet_name: str, key: str) -> dict:
        snapshot = self.get_current()
        if snapshot is None:
            return {}
        return snapshot.get_month_metadata(sheet_name, key)

    def set_month_metadata(self, sheet_name: str, key: str, value: dict) -> None:
        snapshot = self.get_current()
        if snapshot is None:
            raise KeyError(sheet_name)
        self.repository.save(snapshot.set_month_metadata(sheet_name, key, value))

    def clear_month_metadata(self, sheet_name: str, key: str) -> bool:
        snapshot = self.get_current()
        if snapshot is None:
            return False
        current = snapshot.get_month_metadata(sheet_name, key)
        if not current:
            return False
        self.repository.save(snapshot.clear_month_metadata(sheet_name, key))
        return True

    def current_workbook_path(self) -> Optional[Path]:
        snapshot = self.get_current()
        if snapshot is None:
            return None
        path = UPLOAD_DIR / snapshot.stored_filename
        if not path.exists():
            return None
        return path

    def clear_current(self) -> None:
        self.repository.clear()
