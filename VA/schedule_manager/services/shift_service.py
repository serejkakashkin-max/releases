import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from VA.schedule_manager.models.shift import ShiftDefinition
from VA.schedule_manager.parsers.monthly_workbook_parser import workbook_usage_index
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.schedule_service import ScheduleService


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ShiftValidationError(Exception):
    pass


class ShiftInUseError(Exception):
    pass


@dataclass(frozen=True)
class ShiftUsage:
    sheet_name: str
    title: str
    cells_count: int


DEFAULT_SHIFTS = [
    ShiftDefinition("Праздник", "Праздник", "П", "Нерабочий праздничный день", "#D92D20", 0, "МСК", aliases=("П",)),
    ShiftDefinition("отпуск", "Отпуск", "О", "Отпуск сотрудника", "#667085", 0, "МСК", aliases=("О",)),
    ShiftDefinition("ВХ", "Привлечение в выходной", "ВХ", "Работа в выходной день", "#FFC000", 1, "МСК", "09:00", "10:00"),
    ShiftDefinition("ХД", "Хабаровск дежурный", "ХД", "Хабаровск дежурный", "#FEFF00", 8, "Хаб", "00:00", "09:00"),
    ShiftDefinition("ХР", "Хабаровск основная смена", "ХР", "Хабаровск резервный/основная смена", "#D9D9D9", 8, "Хаб", "02:00", "11:00"),
    ShiftDefinition("8", "МСК основная смена", "8", "Основная дневная смена МСК", "#E7F0FD", 8, "МСК", "09:00", "18:00"),
    ShiftDefinition("ДД", "МСК дневной дежурный", "ДД", "Дневной дежурный МСК", "#92D050", 8, "МСК", "08:00", "17:00"),
    ShiftDefinition("ДР", "МСК резервный дежурный", "ДР", "Дневной резерв МСК", "#C6E0B4", 8, "МСК", "09:00", "18:00"),
    ShiftDefinition("ВД", "МСК вечерний дежурный", "ВД", "Вечерний дежурный МСК", "#00B0F0", 8, "МСК", "16:00", "01:00"),
    ShiftDefinition("ВР", "МСК вечерний резервный", "ВР", "Вечерний резерв МСК", "#7030A0", 8, "МСК", "16:00", "01:00"),
]

DEFAULT_ALIASES = {shift.code: shift.aliases for shift in DEFAULT_SHIFTS}


class ShiftService:
    def __init__(
        self,
        repository: ShiftRepository,
        workbook_path: Optional[Path] = None,
        schedule_service: Optional[ScheduleService] = None,
    ) -> None:
        self.repository = repository
        self.workbook_path = workbook_path
        self.schedule_service = schedule_service

    def list_shifts(self) -> List[ShiftDefinition]:
        shifts = self.repository.load_all()
        if shifts:
            return self._sorted([self._with_default_aliases(shift) for shift in shifts])
        self.repository.save_all(DEFAULT_SHIFTS)
        return self._sorted(DEFAULT_SHIFTS)

    def lookup(self) -> Dict[str, ShiftDefinition]:
        result: Dict[str, ShiftDefinition] = {}
        for shift in self.list_shifts():
            for code in (shift.code, shift.short_name, *shift.aliases):
                normalized = self._normalize_code(code)
                if normalized:
                    result[normalized] = shift
                    result[normalized.lower()] = shift
        return result

    def add_shift(self, data: dict) -> None:
        shifts = self.list_shifts()
        shift = self._build_shift(data)
        if self._find(shifts, shift.code) is not None:
            raise ShiftValidationError("Смена с таким кодом уже есть.")
        shifts.append(shift)
        self.repository.save_all(self._sorted(shifts))

    def update_shift(self, original_code: str, data: dict) -> None:
        shifts = self.list_shifts()
        shift = self._build_shift(data)
        updated: List[ShiftDefinition] = []
        found = False
        for current in shifts:
            if current.code == original_code:
                updated.append(shift)
                found = True
            else:
                if current.code == shift.code:
                    raise ShiftValidationError("Смена с таким кодом уже есть.")
                updated.append(current)

        if not found:
            raise ShiftValidationError("Смена не найдена.")
        self.repository.save_all(self._sorted(updated))

    def delete_shift(self, code: str) -> None:
        usage = self.find_usage(code)
        if usage:
            raise ShiftInUseError("Смена используется в графиках. Удаление заблокировано.")
        shifts = [shift for shift in self.list_shifts() if shift.code != code]
        self.repository.save_all(shifts)

    def reset_defaults(self) -> None:
        self.repository.save_all(DEFAULT_SHIFTS)

    def find_usage(self, code: str) -> List[ShiftUsage]:
        normalized_code = self._normalize_code(code)
        if self.schedule_service is not None:
            return self._find_saved_schedule_usage(normalized_code)
        if not normalized_code or self.workbook_path is None or not self.workbook_path.exists():
            return []

        target_codes = self._usage_codes(normalized_code)
        usage: List[ShiftUsage] = []
        for sheet_usage in workbook_usage_index(self.workbook_path):
            cells_count = 0
            for code, count in sheet_usage.shift_counts:
                if self._normalize_code(code) in target_codes:
                    cells_count += count
            if cells_count:
                usage.append(ShiftUsage(sheet_usage.sheet_name, sheet_usage.label, cells_count))
        return usage

    def _find_saved_schedule_usage(self, code: str) -> List[ShiftUsage]:
        if not code:
            return []
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return []

        target_codes = self._usage_codes(code)
        usage = []
        for option in snapshot.month_options():
            grid = snapshot.get_month_grid(option["sheet_name"])
            cells_count = 0
            for row in grid.employees:
                for current_code in row.assignments.values():
                    if self._normalize_code(current_code) in target_codes:
                        cells_count += 1
            if cells_count:
                usage.append(ShiftUsage(option["sheet_name"], option["label"], cells_count))
        return usage

    def _build_shift(self, data: dict) -> ShiftDefinition:
        name = " ".join(str(data.get("name", "")).strip().split())
        short_name = " ".join(str(data.get("short_name", "")).strip().split())
        code = self._normalize_code(data.get("code", "")) or self._normalize_code(short_name)
        description = " ".join(str(data.get("description", "")).strip().split())
        color = str(data.get("color", "")).strip()
        timezone = " ".join(str(data.get("timezone", "")).strip().split())
        start_time = str(data.get("start_time", "")).strip()
        end_time = str(data.get("end_time", "")).strip()

        if not code:
            raise ShiftValidationError("Сокращение смены обязательно.")
        if not name:
            raise ShiftValidationError("Название смены обязательно.")
        if not HEX_COLOR_RE.match(color):
            raise ShiftValidationError("Цвет должен быть в формате #RRGGBB.")
        if not timezone:
            raise ShiftValidationError("Часовой пояс обязателен.")

        try:
            hours = float(str(data.get("hours", "")).replace(",", "."))
        except ValueError as exc:
            raise ShiftValidationError("Часы должны быть числом.") from exc
        if hours < 0:
            raise ShiftValidationError("Часы не могут быть отрицательными.")

        return ShiftDefinition(
            code=code,
            name=name,
            short_name=short_name,
            description=description,
            color=color,
            hours=hours,
            timezone=timezone,
            start_time=start_time,
            end_time=end_time,
            aliases=tuple(
                self._normalize_code(alias)
                for alias in str(data.get("aliases", "")).split(",")
                if self._normalize_code(alias)
            ),
        )

    def _find(self, shifts: List[ShiftDefinition], code: str) -> Optional[ShiftDefinition]:
        normalized_code = self._normalize_code(code)
        return next((shift for shift in shifts if self._normalize_code(shift.code) == normalized_code), None)

    def _normalize_code(self, value: object) -> str:
        if value is None:
            return ""
        return " ".join(str(value).strip().split())

    def _sorted(self, shifts: List[ShiftDefinition]) -> List[ShiftDefinition]:
        order = {shift.code: index for index, shift in enumerate(DEFAULT_SHIFTS)}
        return sorted(shifts, key=lambda shift: (order.get(shift.code, 999), shift.code))

    def _with_default_aliases(self, shift: ShiftDefinition) -> ShiftDefinition:
        aliases = tuple(dict.fromkeys((*shift.aliases, *DEFAULT_ALIASES.get(shift.code, ()))))
        if aliases == shift.aliases:
            return shift
        return ShiftDefinition(
            code=shift.code,
            name=shift.name,
            short_name=shift.short_name,
            description=shift.description,
            color=shift.color,
            hours=shift.hours,
            timezone=shift.timezone,
            start_time=shift.start_time,
            end_time=shift.end_time,
            aliases=aliases,
        )

    def _usage_codes(self, code: str) -> set:
        shift = self.lookup().get(code) or self.lookup().get(code.lower())
        if shift is None:
            return {code}
        return {
            normalized
            for normalized in (
                self._normalize_code(shift.code),
                self._normalize_code(shift.short_name),
                *(self._normalize_code(alias) for alias in shift.aliases),
            )
            if normalized
        }
