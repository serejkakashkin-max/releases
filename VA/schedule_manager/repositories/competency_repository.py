from pathlib import Path
from typing import List

from VA.schedule_manager.models.competency import Competency
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import COMPETENCIES_DATA_FILE


class CompetencyRepository:
    def __init__(self, data_file: Path = COMPETENCIES_DATA_FILE) -> None:
        self.data_file = data_file
        self.store = JsonFileStore(data_file, "competencies")

    def load_all(self) -> List[Competency]:
        data = self.store.load()
        if data is None:
            return []

        return [Competency.from_dict(item) for item in data.get("competencies", [])]

    def save_all(self, competencies: List[Competency]) -> None:
        self.store.save({"competencies": [competency.to_dict() for competency in competencies]})
