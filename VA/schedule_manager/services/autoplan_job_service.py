from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from threading import Lock, Thread
from typing import Callable, Dict, Optional
from uuid import uuid4


@dataclass
class AutoplanJobStep:
    message: str
    status: str = "done"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


@dataclass
class AutoplanJob:
    job_id: str
    sheet_name: str
    year: str
    month: str
    status: str = "running"
    steps: list[AutoplanJobStep] = field(default_factory=list)
    result: Optional[dict] = None
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    finished_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["steps"] = [asdict(step) for step in self.steps]
        return data


class AutoplanJobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, AutoplanJob] = {}
        self._lock = Lock()

    def start(
        self,
        sheet_name: str,
        year: str,
        month: str,
        vacations_confirmed: bool,
        runner: Callable[[str, bool, Callable[[str], None]], object],
    ) -> AutoplanJob:
        job = AutoplanJob(
            job_id=uuid4().hex,
            sheet_name=sheet_name,
            year=year,
            month=month,
        )
        self._save(job)

        thread = Thread(
            target=self._run,
            args=(job.job_id, sheet_name, vacations_confirmed, runner),
            daemon=True,
        )
        thread.start()
        return job

    def get(self, job_id: str) -> Optional[AutoplanJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(
        self,
        job_id: str,
        sheet_name: str,
        vacations_confirmed: bool,
        runner: Callable[[str, bool, Callable[[str], None]], object],
    ) -> None:
        self.add_step(job_id, "Задача автопланирования поставлена в работу.")
        try:
            result = runner(sheet_name, vacations_confirmed, lambda message: self.add_step(job_id, message))
            self.finish(
                job_id,
                {
                    "sheet_name": result.sheet_name,
                    "title": result.title,
                    "assigned_cells_count": result.assigned_cells_count,
                    "violation_count": result.violation_count,
                },
            )
        except Exception as exc:
            self.fail(job_id, str(exc))

    def add_step(self, job_id: str, message: str, status: str = "done") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.steps.append(AutoplanJobStep(message=message, status=status))

    def finish(self, job_id: str, result: dict) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "done"
            job.result = result
            job.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job.steps.append(AutoplanJobStep(message="График сохранен, проверка правил завершена."))

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "error"
            job.error = error
            job.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job.steps.append(AutoplanJobStep(message=error, status="error"))

    def _save(self, job: AutoplanJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job


autoplan_jobs = AutoplanJobManager()
