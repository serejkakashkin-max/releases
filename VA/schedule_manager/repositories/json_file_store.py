import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

import portalocker

from VA.schedule_manager.config import BACKUP_DIR, LOCK_DIR


SCHEMA_VERSION = 1


class JsonFileStore:
    def __init__(self, data_file: Path, schema_name: str) -> None:
        self.data_file = data_file
        self.schema_name = schema_name

    def load(self) -> Optional[dict]:
        with self._lock():
            return self._load_unlocked()

    def save(self, payload: dict) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            self._backup_existing("save")
            self._atomic_write(
                {
                    "schema_version": SCHEMA_VERSION,
                    "schema_name": self.schema_name,
                    "saved_at": datetime.now().isoformat(timespec="seconds"),
                    "payload": payload,
                }
            )

    def clear(self) -> None:
        with self._lock():
            if not self.data_file.exists():
                return
            self._backup_existing("clear")
            self.data_file.unlink()

    def _lock(self):
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = LOCK_DIR / f"{self.schema_name}.lock"
        return portalocker.Lock(str(lock_path), timeout=15)

    def _load_unlocked(self) -> Optional[dict]:
        if not self.data_file.exists():
            return None

        try:
            with self.data_file.open("r", encoding="utf-8-sig") as file:
                data = json.load(file)
        except json.JSONDecodeError:
            recovered = self._recover_latest_backup()
            if recovered is None:
                return None
            data = recovered

        if self._is_versioned(data):
            return data.get("payload", {})
        return data

    def _atomic_write(self, data: dict) -> None:
        temp_file = self.data_file.with_name(f".{self.data_file.name}.{uuid4().hex}.tmp")
        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_file, self.data_file)

    def _backup_existing(self, reason: str) -> None:
        if not self.data_file.exists():
            return
        backup_dir = BACKUP_DIR / self.data_file.stem
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_file = backup_dir / f"{timestamp}_{reason}_{self.data_file.name}"
        shutil.copy2(self.data_file, backup_file)

    def _recover_latest_backup(self) -> Optional[dict]:
        backup_dir = BACKUP_DIR / self.data_file.stem
        if not backup_dir.exists():
            return None
        backups = sorted(
            backup_dir.glob(f"*_{self.data_file.name}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for backup in backups:
            try:
                with backup.open("r", encoding="utf-8-sig") as file:
                    return json.load(file)
            except Exception:
                continue
        return None

    def _is_versioned(self, data: object) -> bool:
        return isinstance(data, dict) and "schema_version" in data and "payload" in data
