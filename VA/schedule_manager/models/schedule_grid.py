from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ScheduleDay:
    day: int
    weekday: str
    date: date


@dataclass(frozen=True)
class ScheduleRow:
    employee_name: str
    hours: Optional[int]
    assignments: Dict[int, str]


@dataclass(frozen=True)
class ScheduleGrid:
    title: str
    year: int
    month: int
    days: List[ScheduleDay]
    employees: List[ScheduleRow]

