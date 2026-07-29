from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from VA.schedule_manager.models.competency import Competency
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import COMPETENCIES_DATA_FILE


class CompetencyRepositoryConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompetencySnapshot:
    status: str
    etag: str
    competencies: tuple[Competency, ...]


class CompetencyRepository:
    def __init__(self, data_file: Path = COMPETENCIES_DATA_FILE) -> None:
        self.data_file = Path(data_file)
        self.store = JsonFileStore(self.data_file, "competencies")

    def load_all(self) -> List[Competency]:
        return list(self.read_snapshot().competencies)

    def read_snapshot(self) -> CompetencySnapshot:
        if not self.data_file.exists():
            return CompetencySnapshot("missing", "missing", ())
        with self.store._lock():
            return self._read_snapshot_unlocked()

    def save_all(
        self,
        competencies: List[Competency],
        *,
        expected_etag: Optional[str] = None,
    ) -> CompetencySnapshot:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        with self.store._lock():
            current = self._read_snapshot_unlocked()
            if expected_etag is not None and str(expected_etag) != current.etag:
                raise CompetencyRepositoryConflictError(
                    "Competency directory was changed by another process."
                )
            normalized = self._validate_competencies(competencies)
            self.store._backup_existing("save")
            self.store._atomic_write(
                {
                    "schema_version": 1,
                    "schema_name": "competencies",
                    "saved_at": datetime.now().isoformat(timespec="seconds"),
                    "payload": {
                        "competencies": [
                            competency.to_dict() for competency in normalized
                        ]
                    },
                }
            )
            return self._read_snapshot_unlocked()

    def _read_snapshot_unlocked(self) -> CompetencySnapshot:
        try:
            raw = self.data_file.read_bytes()
        except FileNotFoundError:
            return CompetencySnapshot("missing", "missing", ())
        etag = "sha256:" + hashlib.sha256(raw).hexdigest()
        if not raw.strip():
            return CompetencySnapshot("empty", etag, ())
        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            return CompetencySnapshot("invalid", etag, ())
        if not isinstance(document, dict):
            return CompetencySnapshot("invalid", etag, ())
        if "schema_version" in document or "payload" in document:
            if (
                document.get("schema_version") != 1
                or document.get("schema_name") != "competencies"
                or not isinstance(document.get("payload"), dict)
            ):
                return CompetencySnapshot("invalid", etag, ())
            payload = document["payload"]
        else:
            payload = document
        rows = payload.get("competencies")
        if not isinstance(rows, list):
            return CompetencySnapshot("invalid", etag, ())
        try:
            competencies = self._validate_competencies(
                [Competency.from_dict(item) for item in rows if isinstance(item, dict)]
            )
        except ValueError:
            return CompetencySnapshot("invalid", etag, ())
        if len(competencies) != len(rows):
            return CompetencySnapshot("invalid", etag, ())
        return CompetencySnapshot("available", etag, tuple(competencies))

    @staticmethod
    def _validate_competencies(
        competencies: List[Competency],
    ) -> List[Competency]:
        result = []
        seen = set()
        for competency in competencies:
            code = str(competency.code or "").strip()
            name = " ".join(str(competency.name or "").strip().split())
            description = " ".join(
                str(competency.description or "").strip().split()
            )
            if not code or not name or code in seen:
                raise ValueError("Invalid competency directory.")
            seen.add(code)
            result.append(
                Competency(code, name, description, bool(competency.is_system))
            )
        return result
