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

    def load_diagnostic(
        self,
        *,
        allow_backup_preview: bool = False,
        allow_legacy_current: bool = False,
    ) -> dict:
        """Read the current file under the normal VA lock without recovery writes."""
        if not self.data_file.exists():
            return self._diagnostic_result("current_missing")
        try:
            if not self.data_file.read_bytes().strip():
                return self._diagnostic_result("current_empty")
        except OSError:
            return self._diagnostic_result("current_invalid")

        with self._lock():
            try:
                with self.data_file.open("r", encoding="utf-8-sig") as file:
                    data = json.load(file)
            except (OSError, UnicodeError, json.JSONDecodeError):
                if allow_backup_preview:
                    recovered = self._recover_latest_backup()
                    if recovered is not None:
                        return self._diagnostic_from_data(
                            recovered,
                            status="recovered_backup",
                            recovery_used=True,
                            allow_legacy_current=allow_legacy_current,
                        )
                return self._diagnostic_result("current_invalid")
            return self._diagnostic_from_data(
                data,
                allow_legacy_current=allow_legacy_current,
            )

    def _diagnostic_from_data(
        self,
        data: object,
        *,
        status: str = "current_valid",
        recovery_used: bool = False,
        allow_legacy_current: bool = False,
    ) -> dict:
        if not isinstance(data, dict):
            return self._diagnostic_result("current_invalid")
        if not self._is_versioned(data):
            if allow_legacy_current:
                return {
                    "status": status,
                    "payload": data,
                    "schema_version": 0,
                    "schema_name": self.schema_name,
                    "saved_at": "",
                    "recovery_used": recovery_used,
                    "legacy_schema": True,
                }
            return self._diagnostic_result("unsupported_schema")
        schema_version = data.get("schema_version")
        schema_name = str(data.get("schema_name") or "")
        payload = data.get("payload")
        if schema_version != SCHEMA_VERSION or schema_name != self.schema_name:
            return self._diagnostic_result(
                "unsupported_schema",
                schema_version=schema_version,
                schema_name=schema_name,
            )
        if not isinstance(payload, dict):
            return self._diagnostic_result("current_invalid")
        return {
            "status": status,
            "payload": payload,
            "schema_version": schema_version,
            "schema_name": schema_name,
            "saved_at": str(data.get("saved_at") or ""),
            "recovery_used": recovery_used,
            "legacy_schema": False,
        }

    def _diagnostic_result(
        self,
        status: str,
        *,
        schema_version: object = None,
        schema_name: str = "",
    ) -> dict:
        return {
            "status": status,
            "payload": None,
            "schema_version": schema_version,
            "schema_name": schema_name,
            "saved_at": "",
            "recovery_used": False,
            "legacy_schema": False,
        }

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
