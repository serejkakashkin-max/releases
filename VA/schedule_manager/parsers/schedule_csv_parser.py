import csv
from datetime import date
from io import StringIO
from pathlib import Path
from typing import List, Optional

from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow


MONTHS_RU = {
    "Январь": 1,
    "Февраль": 2,
    "Март": 3,
    "Апрель": 4,
    "Май": 5,
    "Июнь": 6,
    "Июль": 7,
    "Август": 8,
    "Сентябрь": 9,
    "Октябрь": 10,
    "Ноябрь": 11,
    "Декабрь": 12,
}

TIME_ROW_CODES = {"8", "ДД", "ДР", "ВД", "ВР", "ХД", "ХР"}


class ScheduleCsvParseError(Exception):
    pass


def parse_schedule_csv_file(path: Path) -> ScheduleGrid:
    return parse_schedule_csv(path.read_text(encoding="utf-8"))


def parse_schedule_csv(text: str) -> ScheduleGrid:
    rows = _read_rows(text)
    if len(rows) < 2:
        raise ScheduleCsvParseError("CSV-график слишком короткий.")

    title = rows[0][0].strip()
    month, year = _parse_title(title)
    weekdays = rows[0][3:]
    day_numbers = [_parse_day(value) for value in rows[1][3:]]
    days = [
        ScheduleDay(day=day, weekday=weekday.strip(), date=date(year, month, day))
        for day, weekday in zip(day_numbers, weekdays)
        if day is not None
    ]

    employees: List[ScheduleRow] = []
    for row in rows[2:]:
        name = row[0].strip()
        if not name:
            break
        if name in TIME_ROW_CODES:
            break

        hours = _parse_int(row[2] if len(row) > 2 else "")
        values = row[3 : 3 + len(days)]
        assignments = {
            schedule_day.day: (values[index].strip() if index < len(values) else "")
            for index, schedule_day in enumerate(days)
        }
        employees.append(ScheduleRow(employee_name=_normalize_name(name), hours=hours, assignments=assignments))

    if not employees:
        raise ScheduleCsvParseError("В CSV-графике не найдены строки сотрудников.")

    return ScheduleGrid(title=title, year=year, month=month, days=days, employees=employees)


def _read_rows(text: str) -> List[List[str]]:
    return [row for row in csv.reader(StringIO(text), delimiter=";") if row]


def _parse_title(title: str) -> tuple[int, int]:
    if "-" not in title:
        raise ScheduleCsvParseError(f"Не удалось определить месяц и год из '{title}'.")
    month_name, year_text = title.split("-", 1)
    month = MONTHS_RU.get(month_name.strip())
    if month is None:
        raise ScheduleCsvParseError(f"Неизвестный месяц '{month_name}'.")
    try:
        year = int(year_text.strip())
    except ValueError as exc:
        raise ScheduleCsvParseError(f"Неизвестный год '{year_text}'.") from exc
    return month, year


def _parse_day(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except ValueError:
        return None


def _parse_int(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except ValueError:
        return None


def _normalize_name(name: str) -> str:
    return " ".join(name.split())
