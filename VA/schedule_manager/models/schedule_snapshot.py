from dataclasses import asdict, dataclass, field
from datetime import date
from typing import List

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow


@dataclass(frozen=True)
class ScheduleSnapshot:
    employees: List[Employee]
    original_filename: str
    stored_filename: str
    uploaded_at: str
    month_schedules: List[dict] = field(default_factory=list)

    @property
    def employee_count(self) -> int:
        return len(self.employees)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["employee_count"] = self.employee_count
        return data

    def month_options(self) -> List[dict]:
        return [
            {
                "year": month["year"],
                "month": month["month"],
                "month_name": month["month_name"],
                "sheet_name": month["sheet_name"],
                "label": month["label"],
            }
            for month in self.month_schedules
        ]

    def get_month_grid(self, sheet_name: str) -> ScheduleGrid:
        for month in self.month_schedules:
            if month["sheet_name"] == sheet_name:
                return grid_from_dict(month["grid"])
        raise KeyError(sheet_name)

    def replace_month_grid(self, sheet_name: str, grid: ScheduleGrid) -> "ScheduleSnapshot":
        months = []
        found = False
        for month in self.month_schedules:
            if month["sheet_name"] == sheet_name:
                updated = dict(month)
                updated["grid"] = grid_to_dict(grid)
                months.append(updated)
                found = True
            else:
                months.append(month)
        if not found:
            raise KeyError(sheet_name)
        return ScheduleSnapshot(
            employees=self.employees,
            original_filename=self.original_filename,
            stored_filename=self.stored_filename,
            uploaded_at=self.uploaded_at,
            month_schedules=months,
        )

    def get_month_metadata(self, sheet_name: str, key: str) -> dict:
        for month in self.month_schedules:
            if month["sheet_name"] == sheet_name:
                value = month.get(key)
                return value if isinstance(value, dict) else {}
        raise KeyError(sheet_name)

    def set_month_metadata(self, sheet_name: str, key: str, value: dict) -> "ScheduleSnapshot":
        months = []
        found = False
        for month in self.month_schedules:
            if month["sheet_name"] == sheet_name:
                updated = dict(month)
                updated[key] = value
                months.append(updated)
                found = True
            else:
                months.append(month)
        if not found:
            raise KeyError(sheet_name)
        return ScheduleSnapshot(
            employees=self.employees,
            original_filename=self.original_filename,
            stored_filename=self.stored_filename,
            uploaded_at=self.uploaded_at,
            month_schedules=months,
        )

    def clear_month_metadata(self, sheet_name: str, key: str) -> "ScheduleSnapshot":
        months = []
        found = False
        for month in self.month_schedules:
            if month["sheet_name"] == sheet_name:
                updated = dict(month)
                updated.pop(key, None)
                months.append(updated)
                found = True
            else:
                months.append(month)
        if not found:
            raise KeyError(sheet_name)
        return ScheduleSnapshot(
            employees=self.employees,
            original_filename=self.original_filename,
            stored_filename=self.stored_filename,
            uploaded_at=self.uploaded_at,
            month_schedules=months,
        )

    def add_month_grid(self, sheet_name: str, month_name: str, grid: ScheduleGrid) -> "ScheduleSnapshot":
        months = [
            month
            for month in self.month_schedules
            if not (int(month["year"]) == grid.year and int(month["month"]) == grid.month)
        ]
        months.append(
            {
                "year": grid.year,
                "month": grid.month,
                "month_name": month_name,
                "sheet_name": sheet_name,
                "label": sheet_name,
                "grid": grid_to_dict(grid),
            }
        )
        months = sorted(months, key=lambda item: (int(item["year"]), int(item["month"])), reverse=True)
        return ScheduleSnapshot(
            employees=self.employees,
            original_filename=self.original_filename,
            stored_filename=self.stored_filename,
            uploaded_at=self.uploaded_at,
            month_schedules=months,
        )

    def remove_month_grid(self, sheet_name: str) -> "ScheduleSnapshot":
        months = [month for month in self.month_schedules if month["sheet_name"] != sheet_name]
        if len(months) == len(self.month_schedules):
            raise KeyError(sheet_name)
        return ScheduleSnapshot(
            employees=self.employees,
            original_filename=self.original_filename,
            stored_filename=self.stored_filename,
            uploaded_at=self.uploaded_at,
            month_schedules=months,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleSnapshot":
        employees = [Employee.from_dict(item) for item in data.get("employees", [])]
        return cls(
            employees=employees,
            original_filename=data["original_filename"],
            stored_filename=data["stored_filename"],
            uploaded_at=data["uploaded_at"],
            month_schedules=data.get("month_schedules", []),
        )


def grid_to_dict(grid: ScheduleGrid) -> dict:
    return {
        "title": grid.title,
        "year": grid.year,
        "month": grid.month,
        "days": [
            {
                "day": day.day,
                "weekday": day.weekday,
                "date": day.date.isoformat(),
            }
            for day in grid.days
        ],
        "employees": [
            {
                "employee_name": row.employee_name,
                "hours": row.hours,
                "assignments": {str(day): code for day, code in row.assignments.items()},
            }
            for row in grid.employees
        ],
    }


def grid_from_dict(data: dict) -> ScheduleGrid:
    return ScheduleGrid(
        title=data["title"],
        year=int(data["year"]),
        month=int(data["month"]),
        days=[
            ScheduleDay(
                day=int(item["day"]),
                weekday=item["weekday"],
                date=date.fromisoformat(item["date"]),
            )
            for item in data.get("days", [])
        ],
        employees=[
            ScheduleRow(
                employee_name=item["employee_name"],
                hours=item.get("hours"),
                assignments={int(day): code for day, code in item.get("assignments", {}).items()},
            )
            for item in data.get("employees", [])
        ],
    )
