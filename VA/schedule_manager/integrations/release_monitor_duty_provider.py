from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from calendar import month_name
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

from VA.schedule_manager.config import SCHEDULE_DATA_FILE, SHIFTS_DATA_FILE
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.services.shift_service import DEFAULT_SHIFTS


PROVIDER_CONTRACT = "provider-contract:v1"
MAPPING_RULES = "mapping-rules:v1"
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
EXCLUDED_CODES = {"ДД", "ВД", "ВР", "ХД", "ХР"}
RESERVE_CODES = {"ДР"}
ABSENCE_CODES = {"отпуск", "отгул", "больничный", "праздник"}


def _signature(path: Path) -> Tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _warning(code: str, **details: object) -> dict:
    result = {"type": code}
    result.update(details)
    return result


class ReleaseMonitorDutyProvider:
    provider_id = "va_schedule_manager"

    def __init__(
        self,
        *,
        release_names: Iterable[str],
        name_matcher: Callable[[object, Iterable[str]], str],
        name_matcher_diagnostic: Callable[[object, Iterable[str]], dict] | None = None,
        schedule_file: Path = SCHEDULE_DATA_FILE,
        shifts_file: Path = SHIFTS_DATA_FILE,
    ) -> None:
        self.release_names = tuple(str(value).strip() for value in release_names if str(value).strip())
        self.name_matcher = name_matcher
        self.name_matcher_diagnostic = name_matcher_diagnostic
        self.schedule_file = Path(schedule_file)
        self.shifts_file = Path(shifts_file)
        self.schedule_store = JsonFileStore(self.schedule_file, "schedule")
        self.shifts_store = JsonFileStore(self.shifts_file, "shifts")
        self._cache_lock = threading.RLock()
        self._cache_signature = None
        self._cache = None

    def get_status(self) -> dict:
        return copy.deepcopy(self._load()["status"])

    def get_release_projection(self) -> dict:
        return copy.deepcopy(self._load()["projection"])

    def get_months(self) -> dict:
        state = self._load()
        status = copy.deepcopy(state["status"])
        status["months"] = copy.deepcopy(state["months"])
        return status

    def get_month(self, year: int, month: int) -> dict:
        state = self._load()
        key = f"{int(year):04d}-{int(month):02d}"
        grid = state["month_grids"].get(key)
        if grid is not None:
            return copy.deepcopy(grid)
        status = state["status"]
        return {
            **copy.deepcopy(status),
            "year": int(year),
            "month": int(month),
            "label": "",
            "days": [],
            "employees": [],
            "shifts": copy.deepcopy(state["effective_shifts"]),
            "warnings": [_warning("month_unavailable")],
        }

    def _load(self) -> dict:
        signatures = (_signature(self.schedule_file), _signature(self.shifts_file))
        with self._cache_lock:
            if self._cache is not None and signatures == self._cache_signature:
                return copy.deepcopy(self._cache)

            state = self._stable_read()
            self._cache_signature = (_signature(self.schedule_file), _signature(self.shifts_file))
            self._cache = state
            return copy.deepcopy(state)

    def _stable_read(self) -> dict:
        for _attempt in range(2):
            before = (_signature(self.schedule_file), _signature(self.shifts_file))
            schedule_result = self.schedule_store.load_diagnostic(allow_backup_preview=True)
            shifts_result = self.shifts_store.load_diagnostic(
                allow_backup_preview=False,
                allow_legacy_current=True,
            )
            after = (_signature(self.schedule_file), _signature(self.shifts_file))
            if before == after:
                return self._build_state(schedule_result, shifts_result)
        return self._unavailable_state("unstable_source")

    def _build_state(self, schedule_result: dict, shifts_result: dict) -> dict:
        source_status = str(schedule_result.get("status") or "current_invalid")
        status_map = {
            "current_missing": "missing_schedule",
            "current_empty": "empty_schedule",
            "current_invalid": "invalid_schedule",
            "unsupported_schema": "unsupported_schema",
            "recovered_backup": "recovered_backup",
        }
        if source_status not in {"current_valid", "recovered_backup"}:
            return self._unavailable_state(status_map.get(source_status, "invalid_schedule"))

        shifts_status = str(shifts_result.get("status") or "current_missing")
        if shifts_status not in {"current_valid", "current_missing"}:
            return self._unavailable_state("invalid_schedule")
        shifts_payload = shifts_result.get("payload") if shifts_status == "current_valid" else None
        try:
            shifts, lookup, shift_warnings = self._validate_shifts(shifts_payload)
            months, schedule_warnings = self._validate_schedule(schedule_result.get("payload"))
        except ValueError as exc:
            return self._unavailable_state(str(exc) or "invalid_schedule")

        revision_payload = {
            "contract": PROVIDER_CONTRACT,
            "schedule": schedule_result.get("payload"),
            "effective_shifts": shifts,
            "mapping_rules": MAPPING_RULES,
        }
        revision = "sha256:" + hashlib.sha256(_canonical_bytes(revision_payload)).hexdigest()
        authoritative = source_status == "current_valid"
        warnings = shift_warnings + schedule_warnings
        projection, month_grids = self._project(months, shifts, lookup, revision, authoritative)
        warnings.extend(projection.get("warnings") or [])
        final_status = "recovered_backup" if not authoritative else ("mapping_warnings" if warnings else "ready")
        projection.update(
            {
                "authoritative": authoritative,
                "status": final_status,
                "revision": revision,
                "updated_at": str(schedule_result.get("saved_at") or ""),
                "warnings": copy.deepcopy(warnings),
                "unavailable_reason": "" if authoritative else final_status,
            }
        )
        status = {
            "provider_id": self.provider_id,
            "available": True,
            "authoritative": authoritative,
            "status": final_status,
            "revision": revision,
            "updated_at": str(schedule_result.get("saved_at") or ""),
            "months_count": len(month_grids),
            "warnings_count": len(warnings),
            "warnings": copy.deepcopy(warnings),
        }
        for grid in month_grids.values():
            grid.update({
                "available": True,
                "status": final_status,
                "authoritative": authoritative,
                "revision": revision,
                "updated_at": status["updated_at"],
            })
        return {
            "status": status,
            "projection": projection,
            "months": projection["months"],
            "month_grids": month_grids,
            "effective_shifts": shifts,
        }

    def _unavailable_state(self, status_name: str) -> dict:
        status = {
            "provider_id": self.provider_id,
            "available": False,
            "authoritative": False,
            "status": status_name,
            "revision": "",
            "updated_at": "",
            "months_count": 0,
            "warnings_count": 0,
            "warnings": [],
        }
        projection = {
            **status,
            "source": self.provider_id,
            "dates": {},
            "availability": {"status": "availability_unknown", "authoritative": False, "candidates": {}},
            "evening": {},
            "months": [],
            "date_states": {},
            "ambiguous_dates": [],
            "unmapped_entries_count": 0,
            "unavailable_reason": status_name,
        }
        return {"status": status, "projection": projection, "months": [], "month_grids": {}, "effective_shifts": []}

    def _validate_schedule(self, payload: object) -> Tuple[List[dict], List[dict]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("month_schedules"), list):
            raise ValueError("invalid_schedule")
        if not payload["month_schedules"]:
            raise ValueError("empty_schedule")
        month_counts = {}
        for raw_month in payload["month_schedules"]:
            if isinstance(raw_month, dict):
                try:
                    key = (int(raw_month.get("year")), int(raw_month.get("month")))
                except (TypeError, ValueError):
                    continue
                month_counts[key] = month_counts.get(key, 0) + 1
        seen_months = set()
        normalized = []
        warnings = []
        for raw_month in payload["month_schedules"]:
            if not isinstance(raw_month, dict):
                raise ValueError("invalid_schedule")
            try:
                year = int(raw_month.get("year"))
                month = int(raw_month.get("month"))
            except (TypeError, ValueError):
                raise ValueError("invalid_schedule")
            if year < 2000 or year > 2100 or month < 1 or month > 12:
                raise ValueError("invalid_schedule")
            key = (year, month)
            if month_counts.get(key, 0) > 1:
                if key not in seen_months:
                    warnings.append(_warning("duplicate_month", month=f"{year:04d}-{month:02d}"))
                seen_months.add(key)
                continue
            seen_months.add(key)
            grid = raw_month.get("grid")
            if not isinstance(grid, dict) or not isinstance(grid.get("days"), list) or not isinstance(grid.get("employees"), list):
                raise ValueError("invalid_schedule")
            days = []
            valid_days = set()
            for raw_day in grid["days"]:
                if not isinstance(raw_day, dict):
                    raise ValueError("invalid_schedule")
                try:
                    day = int(raw_day.get("day"))
                    parsed_date = date.fromisoformat(str(raw_day.get("date") or ""))
                except (TypeError, ValueError):
                    raise ValueError("invalid_schedule")
                if parsed_date.year != year or parsed_date.month != month or parsed_date.day != day:
                    raise ValueError("invalid_schedule")
                valid_days.add(day)
                days.append({"day": day, "weekday": str(raw_day.get("weekday") or ""), "date": parsed_date.isoformat()})
            employees = []
            names_seen = set()
            duplicate_names = set()
            for row_index, raw_row in enumerate(grid["employees"]):
                if not isinstance(raw_row, dict):
                    raise ValueError("invalid_schedule")
                employee_name = " ".join(str(raw_row.get("employee_name") or "").strip().split())
                assignments = raw_row.get("assignments")
                if not employee_name or not isinstance(assignments, dict):
                    raise ValueError("invalid_schedule")
                normalized_name = employee_name.casefold()
                if normalized_name in names_seen:
                    duplicate_names.add(normalized_name)
                    warnings.append(_warning("duplicate_employee_row", month=f"{year:04d}-{month:02d}", row=row_index))
                names_seen.add(normalized_name)
                normalized_assignments = {}
                for raw_day, raw_code in assignments.items():
                    try:
                        day = int(raw_day)
                    except (TypeError, ValueError):
                        raise ValueError("invalid_schedule")
                    if day not in valid_days or not isinstance(raw_code, str):
                        raise ValueError("invalid_schedule")
                    normalized_assignments[str(day)] = raw_code.strip()
                employees.append({
                    "employee_name": employee_name,
                    "hours": raw_row.get("hours"),
                    "assignments": normalized_assignments,
                    "ambiguous": normalized_name in duplicate_names,
                })
            if duplicate_names:
                for row in employees:
                    if row["employee_name"].casefold() in duplicate_names:
                        row["ambiguous"] = True
            normalized.append({
                "year": year,
                "month": month,
                "label": str(raw_month.get("label") or f"{month_name[month]} {year}"),
                "days": days,
                "employees": employees,
            })
        return normalized, warnings

    def _validate_shifts(self, payload: object) -> Tuple[List[dict], Dict[str, str], List[dict]]:
        raw_shifts = payload.get("shifts") if isinstance(payload, dict) else None
        if raw_shifts is None:
            raw_shifts = [shift.to_dict() for shift in DEFAULT_SHIFTS]
        if not isinstance(raw_shifts, list):
            raise ValueError("invalid_schedule")
        shifts = []
        lookup: Dict[str, str] = {}
        codes_seen = set()
        warnings = []
        for raw_shift in raw_shifts:
            if not isinstance(raw_shift, dict):
                raise ValueError("invalid_schedule")
            raw_code = raw_shift.get("code")
            raw_short_name = raw_shift.get("short_name", "")
            aliases = raw_shift.get("aliases", [])
            if (
                not isinstance(raw_code, str)
                or not isinstance(raw_short_name, str)
                or not isinstance(aliases, (list, tuple))
                or any(not isinstance(value, str) for value in aliases)
            ):
                raise ValueError("invalid_schedule")
            code = " ".join(raw_code.strip().split())
            short_name = " ".join(raw_short_name.strip().split())
            code_key = code.casefold()
            if not code or code_key in codes_seen:
                raise ValueError("invalid_schedule")
            codes_seen.add(code_key)
            color = str(raw_shift.get("color") or "")
            safe_color = color if HEX_COLOR_RE.fullmatch(color) else "#64748B"
            normalized = {
                "code": code,
                "name": str(raw_shift.get("name") or code),
                "short_name": short_name,
                "aliases": [str(value).strip() for value in aliases if str(value).strip()],
                "color": safe_color,
            }
            shifts.append(normalized)
            for alias in (code, short_name, *normalized["aliases"]):
                key = alias.casefold()
                if not key:
                    continue
                previous = lookup.get(key)
                if previous and previous != code:
                    warnings.append(_warning("ambiguous_shift_alias"))
                    lookup[key] = ""
                elif key not in lookup:
                    lookup[key] = code
        return shifts, lookup, warnings

    def _project(self, months: List[dict], shifts: List[dict], lookup: Dict[str, str], revision: str, authoritative: bool):
        dates: Dict[str, str] = {}
        availability: Dict[str, dict] = {}
        evening: Dict[str, dict] = {}
        date_states: Dict[str, dict] = {}
        warnings: List[dict] = []
        ambiguous_dates = []
        unmapped_entries = 0
        month_grids = {}

        for month in months:
            key = f"{month['year']:04d}-{month['month']:02d}"
            display_rows = []
            per_date_primary: Dict[str, list] = {}
            per_date_reserve: Dict[str, list] = {}
            duplicate_row_dates = set()
            for row in month["employees"]:
                if self.name_matcher_diagnostic is not None:
                    match_result = self.name_matcher_diagnostic(row["employee_name"], self.release_names)
                    matched = str(match_result.get("name") or "")
                    match_status = str(match_result.get("status") or "unmapped")
                else:
                    matched = self.name_matcher(row["employee_name"], self.release_names)
                    match_status = "matched" if matched else "unmapped"
                display_assignments = {}
                for raw_day, raw_value in row["assignments"].items():
                    day = int(raw_day)
                    date_key = f"{key}-{day:02d}"
                    canonical = lookup.get(str(raw_value).casefold(), None)
                    if canonical == "":
                        warnings.append(_warning("ambiguous_shift_code", month=key, day=day))
                        canonical = None
                    elif canonical is None and raw_value:
                        warnings.append(_warning("unknown_shift_code", month=key, day=day))
                        canonical = raw_value
                    display_assignments[raw_day] = canonical or raw_value
                    if row.get("ambiguous"):
                        if raw_value:
                            duplicate_row_dates.add(date_key)
                        continue
                    if canonical == "ВД":
                        per_date_primary.setdefault(date_key, []).append(matched or f"__{match_status}__")
                        if not matched:
                            unmapped_entries += 1
                    elif canonical == "ВР":
                        if matched:
                            per_date_reserve.setdefault(date_key, []).append(matched)
                        else:
                            unmapped_entries += 1
                    if matched:
                        category, reason = self._classify(canonical or raw_value)
                        if category in {"excluded", "reserve"}:
                            availability.setdefault(date_key, {})[matched] = {
                                "status": canonical or raw_value,
                                "availability": category,
                                "reason": reason,
                            }
                display_rows.append({
                    "employee_name": row["employee_name"],
                    "hours": row.get("hours"),
                    "assignments": display_assignments,
                    "warning": "duplicate_employee_row" if row.get("ambiguous") else "",
                })

            for day in month["days"]:
                date_key = day["date"]
                primaries = list(dict.fromkeys(per_date_primary.get(date_key) or []))
                reserves = list(dict.fromkeys(per_date_reserve.get(date_key) or []))
                valid_primaries = [value for value in primaries if value and not value.startswith("__")]
                state = "no_duty"
                reviewer = ""
                state_warnings = []
                if date_key in duplicate_row_dates:
                    state = "ambiguous"
                    state_warnings.append("duplicate_employee_row")
                elif len(primaries) == 1 and len(valid_primaries) == 1:
                    reviewer = valid_primaries[0]
                    state = "ready"
                elif primaries and not valid_primaries:
                    state = "ambiguous" if "__ambiguous__" in primaries else "unmapped"
                    state_warnings.append("ambiguous_employee" if state == "ambiguous" else "unmapped_employee")
                elif len(primaries) > 1:
                    state = "ambiguous"
                    state_warnings.append("multiple_primary_evening")
                if state == "ambiguous":
                    ambiguous_dates.append(date_key)
                    availability.pop(date_key, None)
                if date.fromisoformat(date_key).weekday() >= 5 and state == "ready":
                    reviewer = ""
                    state = "no_duty"
                if reviewer:
                    dates[date_key] = reviewer
                date_states[date_key] = {
                    "date": date_key,
                    "state": state if authoritative else "provider_unavailable",
                    "reviewer": reviewer if authoritative else "",
                    "primary_evening": valid_primaries,
                    "reserve_evening": reserves,
                    "warnings": state_warnings,
                }
                evening[date_key] = {"primary": valid_primaries, "reserve": reserves, "warnings": state_warnings}
            month_grids[key] = {
                "year": month["year"],
                "month": month["month"],
                "label": month["label"],
                "days": copy.deepcopy(month["days"]),
                "employees": display_rows,
                "shifts": copy.deepcopy(shifts),
                "warnings": [],
            }

        months_list = sorted(month_grids)
        projection = {
            "source": self.provider_id,
            "dates": dates if authoritative else {},
            "availability": availability if authoritative else {"status": "availability_unknown", "authoritative": False, "candidates": {}},
            "evening": evening if authoritative else {},
            "months": months_list,
            "date_states": date_states,
            "ambiguous_dates": ambiguous_dates,
            "unmapped_entries_count": unmapped_entries,
            "warnings": warnings,
            "revision": revision,
        }
        return projection, month_grids

    @staticmethod
    def _classify(code: str) -> Tuple[str, str]:
        normalized = str(code or "").strip()
        lowered = normalized.casefold()
        if normalized in EXCLUDED_CODES:
            return "excluded", {
                "ДД": "Дневной дежурный",
                "ВД": "Вечерний дежурный",
                "ВР": "Вечерний резервный дежурный",
                "ХД": "Хабаровская смена",
                "ХР": "Хабаровский резерв",
            }.get(normalized, "Дежурство")
        if lowered in ABSENCE_CODES:
            return "excluded", normalized
        if normalized in RESERVE_CODES:
            return "reserve", "Дневной резервный ответственный"
        return "available", ""
