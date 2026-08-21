from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple


SURNAMES_KEY = "__surname__"


def employee_identity_key(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", str(value or "").casefold())


def employee_surname_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return employee_identity_key(raw.split()[0])


def build_directory_order_index(employee_service=None) -> Dict[str, Any]:
    order_index: Dict[str, Any] = {}
    surname_index: Dict[str, int] = {}

    try:
        from services.employee_directory_service import get_va_members, load_employee_directory_context

        context = load_employee_directory_context()
        if context.status == "available" and context.payload:
            surname_candidates: Dict[str, set] = {}
            for idx, employee in enumerate(get_va_members(context)):
                for name in _employee_directory_names(employee):
                    _put_order_key(order_index, name, idx)

                surname = employee_surname_key(employee.get("release_name") or employee.get("full_name"))
                if surname:
                    surname_candidates.setdefault(surname, set()).add(idx)

            surname_index.update(
                {
                    surname: next(iter(indices))
                    for surname, indices in surname_candidates.items()
                    if len(indices) == 1
                }
            )
    except Exception:
        pass

    if employee_service is not None:
        fallback_idx = (
            max(
                (value for value in order_index.values() if isinstance(value, int)),
                default=-1,
            )
            + 1
        )
        for employee in employee_service.list_employees():
            if getattr(employee, "status", "") != "active":
                continue
            before = len(order_index)
            _put_order_key(order_index, employee.name, fallback_idx)
            surname = employee_surname_key(employee.name)
            if surname and surname not in surname_index:
                surname_index[surname] = fallback_idx
            if len(order_index) > before:
                fallback_idx += 1

        if not order_index:
            for idx, employee in enumerate(employee_service.list_employees()):
                _put_order_key(order_index, employee.name, idx)
                surname = employee_surname_key(employee.name)
                if surname and surname not in surname_index:
                    surname_index[surname] = idx

    order_index[SURNAMES_KEY] = surname_index
    return order_index


def employee_order(name: str, order_index: Dict[str, Any], fallback_position: int) -> int:
    key = employee_identity_key(name)
    if key in order_index:
        return int(order_index[key])
    surname_index = order_index.get(SURNAMES_KEY) or {}
    surname = employee_surname_key(name)
    if surname in surname_index:
        return int(surname_index[surname])
    return fallback_position


def employee_identity_matches(left: str, right: str, order_index: Dict[str, Any] | None = None) -> bool:
    left_key = employee_identity_key(left)
    right_key = employee_identity_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    if not order_index:
        return False
    fallback = len(order_index) + 1
    left_order = employee_order(left, order_index, fallback)
    right_order = employee_order(right, order_index, fallback)
    return left_order != fallback and left_order == right_order


def filter_employees_not_in_grid(active_employees: Iterable[Any], rows: Iterable[Any]) -> List[Any]:
    order_index = build_directory_order_index()
    existing_names = [getattr(row, "employee_name", "") for row in rows]
    result = []
    for employee in active_employees:
        if not any(employee_identity_matches(employee.name, name, order_index) for name in existing_names):
            result.append(employee)
    return result


def sorted_rows_by_directory(rows: Iterable[Any], employee_service=None) -> List[Any]:
    row_list = list(rows)
    order_index = build_directory_order_index(employee_service)
    fallback_position = len(order_index) + 1
    return [
        row
        for _, row in sorted(
            enumerate(row_list),
            key=lambda item: (
                employee_order(
                    getattr(item[1], "employee_name", ""),
                    order_index,
                    fallback_position,
                ),
                item[0],
            ),
        )
    ]


def sorted_dict_rows_by_directory(rows: Iterable[dict]) -> List[dict]:
    row_list = list(rows)
    order_index = build_directory_order_index()
    fallback_position = len(order_index) + 1
    return [
        row
        for _, row in sorted(
            enumerate(row_list),
            key=lambda item: (
                employee_order(
                    item[1].get("employee_name", ""),
                    order_index,
                    fallback_position,
                ),
                item[0],
            ),
        )
    ]


def _employee_directory_names(employee: dict) -> List[str]:
    names = [
        employee.get("release_name"),
        employee.get("full_name"),
    ]
    names.extend(
        alias.get("value")
        for alias in employee.get("aliases") or []
        if alias.get("type") in {"full", "release", "schedule", "va"}
    )
    names.extend(
        source_ref.partition("va:employees:")[2]
        for source_ref in employee.get("source_refs") or []
        if str(source_ref).startswith("va:employees:")
    )
    return [str(name) for name in names if str(name or "").strip()]


def _put_order_key(order_index: Dict[str, Any], name: str, idx: int) -> None:
    key = employee_identity_key(name)
    if key and key not in order_index:
        order_index[key] = idx
