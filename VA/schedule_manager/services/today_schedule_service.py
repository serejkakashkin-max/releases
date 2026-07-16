from dataclasses import dataclass
from datetime import date, datetime, time
from typing import List, Optional

from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.shift_service import ShiftService


PRIMARY_SHIFT_ORDER = ("ВД", "ДД", "ХД")
MONTH_LABELS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


@dataclass(frozen=True)
class TodayShiftGroup:
    code: str
    display_code: str
    name: str
    color: str
    text_color: str
    employees: List[str]


@dataclass(frozen=True)
class TodayScheduleState:
    has_data: bool
    date: date
    title: str = ""
    primary_shift: str = ""
    primary_duty_employee: str = ""
    shifts: List[TodayShiftGroup] = None

    @property
    def date_label(self) -> str:
        return f"{self.date.day} {MONTH_LABELS.get(self.date.month, '')}".strip()

    def to_dict(self) -> dict:
        return {
            "has_data": self.has_data,
            "date": self.date.isoformat(),
            "date_label": self.date_label,
            "title": self.title,
            "primary_shift": self.primary_shift,
            "primary_duty_employee": self.primary_duty_employee,
            "shifts": [
                {
                    "code": shift.code,
                    "display_code": shift.display_code,
                    "name": shift.name,
                    "color": shift.color,
                    "text_color": shift.text_color,
                    "employees": shift.employees,
                }
                for shift in (self.shifts or [])
            ],
        }


class TodayScheduleService:
    def __init__(self, schedule_service: ScheduleService, shift_service: ShiftService) -> None:
        self.schedule_service = schedule_service
        self.shift_service = shift_service

    def get_state(self, moment: Optional[datetime] = None) -> TodayScheduleState:
        moment = moment or datetime.now()
        today = moment.date()
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return TodayScheduleState(False, today, shifts=[])

        month = next(
            (
                option
                for option in snapshot.month_options()
                if option["year"] == today.year and option["month"] == today.month
            ),
            None,
        )
        if month is None:
            return TodayScheduleState(False, today, shifts=[])

        grid = snapshot.get_month_grid(month["sheet_name"])
        if today.day not in {day.day for day in grid.days}:
            return TodayScheduleState(False, today, shifts=[])

        grouped = self._group_assignments(grid, today.day)
        primary_shift = self._current_primary_shift(moment.time(), grouped)
        primary_employee = grouped.get(primary_shift, [""])[0] if primary_shift else ""

        return TodayScheduleState(
            has_data=True,
            date=today,
            title=grid.title,
            primary_shift=primary_shift,
            primary_duty_employee=primary_employee,
            shifts=self._shift_groups(grouped),
        )

    def _group_assignments(self, grid, day: int) -> dict:
        result = {}
        for row in grid.employees:
            code = self._normalize_code(row.assignments.get(day, ""))
            if not code:
                continue
            result.setdefault(code, []).append(row.employee_name)
        return result

    def _shift_groups(self, grouped: dict) -> List[TodayShiftGroup]:
        lookup = self.shift_service.lookup()
        order = {shift.code: index for index, shift in enumerate(self.shift_service.list_shifts())}
        groups = []
        for code, employees in sorted(grouped.items(), key=lambda item: (order.get(item[0], 999), item[0])):
            shift = lookup.get(code) or lookup.get(code.lower())
            groups.append(
                TodayShiftGroup(
                    code=code,
                    display_code=shift.display_code if shift else code,
                    name=shift.name if shift else code,
                    color=shift.color if shift else "",
                    text_color=shift.text_color if shift else "",
                    employees=employees,
                )
            )
        return groups

    def _current_primary_shift(self, current_time: time, grouped: dict) -> str:
        active_primary = [
            code
            for code in PRIMARY_SHIFT_ORDER
            if code in grouped and self._is_shift_active_now(code, current_time)
        ]
        if active_primary:
            return active_primary[0]

        for code in PRIMARY_SHIFT_ORDER:
            if code in grouped:
                return code
        return ""

    def _is_shift_active_now(self, code: str, current_time: time) -> bool:
        shift = self.shift_service.lookup().get(code)
        if shift is None or not shift.start_time or not shift.end_time:
            return True
        start = self._parse_time(shift.start_time)
        end = self._parse_time(shift.end_time)
        if start is None or end is None:
            return True
        if start <= end:
            return start <= current_time < end
        return current_time >= start or current_time < end

    def _parse_time(self, value: str) -> Optional[time]:
        try:
            hour, minute = value.split(":", 1)
            return time(int(hour), int(minute))
        except (TypeError, ValueError):
            return None

    def _normalize_code(self, value: object) -> str:
        return " ".join(str(value or "").strip().split())
