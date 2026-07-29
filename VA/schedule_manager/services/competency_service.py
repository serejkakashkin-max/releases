import re
from typing import List, Optional, Set

from services.cross_process_file_lock import CrossProcessFileLock
from services.runtime_paths import get_coordination_lock_path
from VA.schedule_manager.models.competency import Competency
from VA.schedule_manager.repositories.competency_repository import (
    CompetencyRepository,
    CompetencyRepositoryConflictError,
)
from VA.schedule_manager.repositories.managed_employee_repository import ManagedEmployeeRepository
from VA.schedule_manager.repositories.employee_settings_repository import (
    EmployeeSettingsRepository,
)


COMPETENCY_MANAGER = "manager"
COMPETENCY_SUPPORT = "support"
COMPETENCY_MPR_COORDINATOR = "mpr_coordinator"
COMPETENCY_NEWCOMER = "newcomer"

DEFAULT_COMPETENCIES = [
    Competency(COMPETENCY_MANAGER, "Руководитель", "Руководитель может быть только в смене 8.", True),
    Competency(COMPETENCY_SUPPORT, "Сотрудник сопровождения", "Исполнитель смен сопровождения.", True),
    Competency(
        COMPETENCY_MPR_COORDINATOR,
        "МПР-координатор",
        "Компетенция для правил совместимости МПР в графике.",
        True,
    ),
    Competency(
        COMPETENCY_NEWCOMER,
        "Новичок",
        "В первый месяц не назначается основным дежурным, если в локации есть другие сотрудники.",
        True,
    ),
]

CODE_RE = re.compile(r"^[a-z0-9_]+$")


class CompetencyValidationError(Exception):
    pass


class CompetencyInUseError(Exception):
    pass


class CompetencyConflictError(Exception):
    pass


class CompetencyService:
    def __init__(
        self,
        repository: CompetencyRepository,
        employee_repository: ManagedEmployeeRepository = None,
    ) -> None:
        self.repository = repository
        self.employee_repository = employee_repository or ManagedEmployeeRepository()

    def list_competencies(self) -> List[Competency]:
        snapshot = self.repository.read_snapshot()
        if snapshot.status == "invalid":
            raise CompetencyValidationError(
                "Справочник компетенций повреждён."
            )
        return self._sorted(
            self._merge_defaults(list(snapshot.competencies))
        )

    def admin_snapshot(self) -> dict:
        snapshot = self.repository.read_snapshot()
        competencies = (
            []
            if snapshot.status == "invalid"
            else self._sorted(
                self._merge_defaults(list(snapshot.competencies))
            )
        )
        usage = self.usage_counts()
        return {
            "status": snapshot.status,
            "etag": snapshot.etag,
            "items": [
                {
                    **competency.to_dict(),
                    "usage_count": usage.get(competency.code, 0),
                }
                for competency in competencies
            ],
        }

    def add_competency(
        self,
        data: dict,
        *,
        expected_etag: Optional[str] = None,
    ) -> dict:
        with CrossProcessFileLock(get_coordination_lock_path()):
            snapshot = self.repository.read_snapshot()
            self._require_mutable_snapshot(snapshot.status)
            self._check_expected_etag(snapshot.etag, expected_etag)
            competencies = self._sorted(
                self._merge_defaults(list(snapshot.competencies))
            )
            competency = self._build_competency(data, competencies=competencies)
            if self._find(competencies, competency.code) is not None:
                raise CompetencyValidationError(
                    "Компетенция с таким кодом уже есть."
                )
            competencies.append(competency)
            self._save(competencies, snapshot.etag)
        return self.admin_snapshot()

    def update_competency(
        self,
        original_code: str,
        data: dict,
        *,
        expected_etag: Optional[str] = None,
    ) -> dict:
        with CrossProcessFileLock(get_coordination_lock_path()):
            snapshot = self.repository.read_snapshot()
            self._require_mutable_snapshot(snapshot.status)
            self._check_expected_etag(snapshot.etag, expected_etag)
            competencies = self._sorted(
                self._merge_defaults(list(snapshot.competencies))
            )
            current = self._find(competencies, original_code)
            if current is None:
                raise CompetencyValidationError("Компетенция не найдена.")
            if current.is_system:
                raise CompetencyValidationError(
                    "Системную компетенцию изменять нельзя."
                )
            submitted_code = self._normalize_code(
                data.get("code", original_code)
            )
            if submitted_code != current.code:
                raise CompetencyValidationError(
                    "Код существующей компетенции изменять нельзя."
                )
            competency = self._build_competency(
                data,
                original_code=original_code,
                competencies=competencies,
            )
            updated = [
                competency if item.code == original_code else item
                for item in competencies
            ]
            self._save(updated, snapshot.etag)
        return self.admin_snapshot()

    def delete_competency(
        self,
        code: str,
        *,
        expected_etag: Optional[str] = None,
    ) -> dict:
        with CrossProcessFileLock(get_coordination_lock_path()):
            snapshot = self.repository.read_snapshot()
            self._require_mutable_snapshot(snapshot.status)
            self._check_expected_etag(snapshot.etag, expected_etag)
            competencies = self._sorted(
                self._merge_defaults(list(snapshot.competencies))
            )
            competency = self._find(competencies, code)
            if competency is None:
                raise CompetencyValidationError("Компетенция не найдена.")
            if competency.is_system:
                raise CompetencyValidationError(
                    "Системную компетенцию удалить нельзя."
                )
            settings_snapshot = EmployeeSettingsRepository().read()
            if settings_snapshot.status != "available":
                raise CompetencyValidationError(
                    "Хранилище настроек сотрудников недоступно."
                )
            usage_count = self._usage_counts_from_snapshot(
                settings_snapshot
            ).get(competency.code, 0)
            if usage_count:
                raise CompetencyInUseError(
                    "Компетенция назначена сотрудникам. "
                    f"Количество использований: {usage_count}."
                )
            self._save(
                [item for item in competencies if item.code != competency.code],
                snapshot.etag,
            )
        return self.admin_snapshot()

    def used_by_employees(self, code: str) -> List[str]:
        return [
            employee.name
            for employee in self.employee_repository.load_all()
            if code in set(employee.competencies)
        ]

    def usage_counts(self) -> dict:
        snapshot = EmployeeSettingsRepository().read()
        return self._usage_counts_from_snapshot(snapshot)

    def _usage_counts_from_snapshot(self, snapshot) -> dict:
        employees = (
            (snapshot.payload or {}).get("employees")
            if snapshot.status == "available"
            else {}
        )
        counts = {}
        for settings in (employees or {}).values():
            if not isinstance(settings, dict):
                continue
            for code in set(settings.get("competencies") or []):
                normalized = self._normalize_code(code)
                if normalized:
                    counts[normalized] = counts.get(normalized, 0) + 1
        return counts

    def valid_codes(self) -> Set[str]:
        return {competency.code for competency in self.list_competencies()}

    def _build_competency(
        self,
        data: dict,
        original_code: str = "",
        *,
        competencies: Optional[List[Competency]] = None,
    ) -> Competency:
        code = self._normalize_code(data.get("code", original_code))
        name = " ".join(str(data.get("name", "")).strip().split())
        description = " ".join(str(data.get("description", "")).strip().split())
        current = (
            self._find(competencies or self.list_competencies(), original_code)
            if original_code
            else None
        )

        if not code:
            raise CompetencyValidationError("Код компетенции обязателен.")
        if not CODE_RE.match(code):
            raise CompetencyValidationError("Код компетенции может содержать только латинские буквы, цифры и _.")
        if not name:
            raise CompetencyValidationError("Название компетенции обязательно.")
        return Competency(code, name, description, bool(current and current.is_system))

    def _merge_defaults(self, competencies: List[Competency]) -> List[Competency]:
        by_code = {competency.code: competency for competency in competencies}
        for default in DEFAULT_COMPETENCIES:
            if default.code not in by_code:
                by_code[default.code] = default
        return list(by_code.values())

    def _find(self, competencies: List[Competency], code: str) -> Competency:
        normalized = self._normalize_code(code)
        return next((competency for competency in competencies if competency.code == normalized), None)

    def _normalize_code(self, value: object) -> str:
        return "_".join(str(value or "").strip().lower().split())

    def _sorted(self, competencies: List[Competency]) -> List[Competency]:
        order = {competency.code: index for index, competency in enumerate(DEFAULT_COMPETENCIES)}
        return sorted(competencies, key=lambda competency: (order.get(competency.code, 999), competency.name))

    @staticmethod
    def _check_expected_etag(
        current_etag: str,
        expected_etag: Optional[str],
    ) -> None:
        if expected_etag is not None and str(expected_etag) != current_etag:
            raise CompetencyConflictError(
                "Справочник компетенций был изменён другим процессом."
            )

    def _save(self, competencies: List[Competency], expected_etag: str) -> None:
        try:
            self.repository.save_all(
                self._sorted(competencies),
                expected_etag=expected_etag,
            )
        except CompetencyRepositoryConflictError as exc:
            raise CompetencyConflictError(str(exc)) from exc

    @staticmethod
    def _require_mutable_snapshot(status: str) -> None:
        if status == "invalid":
            raise CompetencyValidationError(
                "Справочник компетенций повреждён."
            )
