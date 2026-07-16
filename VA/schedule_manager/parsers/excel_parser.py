from pathlib import Path
from typing import Iterable, List

import pandas as pd

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.config import EMPLOYEE_SHEET_NAME


SERVICE_WORDS = {
    "дежурный",
    "дежурство",
    "смена",
    "начальник",
    "итого",
    "фио",
    "сотрудник",
}


class ExcelParseError(Exception):
    pass


def parse_employees_from_excel(path: Path) -> List[Employee]:
    try:
        workbook = pd.ExcelFile(path)
    except Exception as exc:
        raise ExcelParseError(f"Не удалось открыть Excel-файл: {exc}") from exc

    if EMPLOYEE_SHEET_NAME not in workbook.sheet_names:
        available = ", ".join(workbook.sheet_names)
        raise ExcelParseError(
            f"Не найден лист '{EMPLOYEE_SHEET_NAME}'. Доступные листы: {available}"
        )

    try:
        frame = pd.read_excel(workbook, sheet_name=EMPLOYEE_SHEET_NAME, header=None)
    except Exception as exc:
        raise ExcelParseError(f"Не удалось прочитать лист '{EMPLOYEE_SHEET_NAME}': {exc}") from exc

    names = _extract_employee_names(frame)
    return [Employee(name=name) for name in names]


def _extract_employee_names(frame: pd.DataFrame) -> List[str]:
    names = set()

    for value in _iter_cells(frame):
        text = _normalize_cell(value)
        if _looks_like_employee_name(text):
            names.add(text)

    return sorted(names)


def _iter_cells(frame: pd.DataFrame) -> Iterable[object]:
    for row in frame.itertuples(index=False):
        for value in row:
            yield value


def _normalize_cell(value: object) -> str:
    if pd.isna(value):
        return ""
    return " ".join(str(value).strip().split())


def _looks_like_employee_name(text: str) -> bool:
    if not text:
        return False
    if not any(char.isalpha() for char in text):
        return False
    if _looks_like_number_or_date(text):
        return False
    lowered = text.lower()
    if any(word in lowered for word in SERVICE_WORDS):
        return False
    return True


def _looks_like_number_or_date(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        pass

    parsed_date = pd.to_datetime(text, errors="coerce")
    return not pd.isna(parsed_date)

