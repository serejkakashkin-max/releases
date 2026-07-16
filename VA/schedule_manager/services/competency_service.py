import re
from typing import List, Set

from VA.schedule_manager.models.competency import Competency
from VA.schedule_manager.repositories.competency_repository import CompetencyRepository
from VA.schedule_manager.repositories.employee_repository import EmployeeRepository


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


class CompetencyService:
    def __init__(
        self,
        repository: CompetencyRepository,
        employee_repository: EmployeeRepository = None,
    ) -> None:
        self.repository = repository
        self.employee_repository = employee_repository or EmployeeRepository()

    def list_competencies(self) -> List[Competency]:
        competencies = self.repository.load_all()
        if competencies:
            return self._sorted(self._merge_defaults(competencies))
        self.repository.save_all(DEFAULT_COMPETENCIES)
        return self._sorted(DEFAULT_COMPETENCIES)

    def add_competency(self, data: dict) -> None:
        competencies = self.list_competencies()
        competency = self._build_competency(data)
        if self._find(competencies, competency.code) is not None:
            raise CompetencyValidationError("Компетенция с таким кодом уже есть.")
        competencies.append(competency)
        self.repository.save_all(self._sorted(competencies))

    def update_competency(self, original_code: str, data: dict) -> None:
        competencies = self.list_competencies()
        competency = self._build_competency(data, original_code=original_code)
        updated = []
        found = False
        for current in competencies:
            if current.code == original_code:
                updated.append(competency)
                found = True
            else:
                if current.code == competency.code:
                    raise CompetencyValidationError("Компетенция с таким кодом уже есть.")
                updated.append(current)

        if not found:
            raise CompetencyValidationError("Компетенция не найдена.")
        self.repository.save_all(self._sorted(updated))

    def delete_competency(self, code: str) -> None:
        competency = self._find(self.list_competencies(), code)
        if competency is None:
            raise CompetencyValidationError("Компетенция не найдена.")
        if competency.is_system:
            raise CompetencyValidationError("Системную компетенцию удалить нельзя.")
        if self.used_by_employees(code):
            raise CompetencyInUseError("Компетенция назначена сотрудникам. Сначала снимите ее в справочнике сотрудников.")
        self.repository.save_all([item for item in self.list_competencies() if item.code != code])

    def used_by_employees(self, code: str) -> List[str]:
        return [
            employee.name
            for employee in self.employee_repository.load_all()
            if code in set(employee.competencies)
        ]

    def valid_codes(self) -> Set[str]:
        return {competency.code for competency in self.list_competencies()}

    def _build_competency(self, data: dict, original_code: str = "") -> Competency:
        code = self._normalize_code(data.get("code", original_code))
        name = " ".join(str(data.get("name", "")).strip().split())
        description = " ".join(str(data.get("description", "")).strip().split())
        current = self._find(self.list_competencies(), original_code) if original_code else None

        if not code:
            raise CompetencyValidationError("Код компетенции обязателен.")
        if not CODE_RE.match(code):
            raise CompetencyValidationError("Код компетенции может содержать только латинские буквы, цифры и _.")
        if not name:
            raise CompetencyValidationError("Название компетенции обязательно.")
        return Competency(code, name, description, bool(current and current.is_system))

    def _merge_defaults(self, competencies: List[Competency]) -> List[Competency]:
        by_code = {competency.code: competency for competency in competencies}
        changed = False
        for default in DEFAULT_COMPETENCIES:
            if default.code not in by_code:
                by_code[default.code] = default
                changed = True
        result = list(by_code.values())
        if changed:
            self.repository.save_all(self._sorted(result))
        return result

    def _find(self, competencies: List[Competency], code: str) -> Competency:
        normalized = self._normalize_code(code)
        return next((competency for competency in competencies if competency.code == normalized), None)

    def _normalize_code(self, value: object) -> str:
        return "_".join(str(value or "").strip().lower().split())

    def _sorted(self, competencies: List[Competency]) -> List[Competency]:
        order = {competency.code: index for index, competency in enumerate(DEFAULT_COMPETENCIES)}
        return sorted(competencies, key=lambda competency: (order.get(competency.code, 999), competency.name))
