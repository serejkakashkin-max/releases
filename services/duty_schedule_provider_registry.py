from __future__ import annotations

import copy
import logging
import threading
from typing import Any, Dict, Optional

from flask import Flask, current_app, has_app_context


LOGGER = logging.getLogger(__name__)
EXTENSION_KEY = "duty_schedule_provider_registry"


def _missing_status(status: str = "missing_provider") -> Dict[str, Any]:
    return {
        "provider_id": "",
        "available": False,
        "authoritative": False,
        "status": status,
        "revision": "",
        "updated_at": "",
        "months_count": 0,
        "warnings_count": 0,
        "warnings": [],
    }


class DutyScheduleProviderRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._provider = None
        self._provider_id = ""

    def register(self, provider: Any) -> None:
        provider_id = str(getattr(provider, "provider_id", "") or "").strip()
        if not provider_id:
            raise ValueError("Duty schedule provider_id is required")
        with self._lock:
            self._provider = provider
            self._provider_id = provider_id

    def unregister(self, provider_id: str) -> None:
        with self._lock:
            if self._provider_id == str(provider_id or "").strip():
                self._provider = None
                self._provider_id = ""

    def status(self) -> Dict[str, Any]:
        return self._call("get_status", _missing_status())

    def revision(self) -> Dict[str, Any]:
        return self._call("get_revision_component", _missing_status())

    def release_projection(self) -> Dict[str, Any]:
        fallback = {
            **_missing_status(),
            "source": "",
            "dates": {},
            "availability": {},
            "evening": {},
            "months": [],
            "date_states": {},
            "ambiguous_dates": [],
            "unmapped_entries_count": 0,
            "unavailable_reason": "missing_provider",
        }
        return self._call("get_release_projection", fallback)

    def months(self) -> Dict[str, Any]:
        fallback = {**_missing_status(), "months": []}
        return self._call("get_months", fallback)

    def month(self, year: int, month: int) -> Dict[str, Any]:
        fallback = {
            **_missing_status(),
            "year": year,
            "month": month,
            "label": "",
            "days": [],
            "employees": [],
            "shifts": [],
        }
        return self._call("get_month", fallback, year, month)

    def _call(self, operation: str, fallback: Dict[str, Any], *args: Any) -> Dict[str, Any]:
        with self._lock:
            provider = self._provider
            provider_id = self._provider_id
        if provider is None:
            return copy.deepcopy(fallback)

        try:
            result = getattr(provider, operation)(*args)
            if not isinstance(result, dict):
                raise TypeError("provider result must be a dictionary")
            return copy.deepcopy(result)
        except Exception as exc:
            LOGGER.exception(
                "Duty schedule provider failed: provider=%s operation=%s error_type=%s",
                provider_id,
                operation,
                type(exc).__name__,
            )
            controlled = copy.deepcopy(fallback)
            controlled.update(
                {
                    "provider_id": provider_id,
                    "available": False,
                    "authoritative": False,
                    "status": "provider_error",
                    "revision": "",
                    "warnings": [],
                    "unavailable_reason": "provider_error",
                }
            )
            return controlled


def init_duty_schedule_provider_registry(app: Flask) -> DutyScheduleProviderRegistry:
    registry = DutyScheduleProviderRegistry()
    app.extensions[EXTENSION_KEY] = registry
    return registry


def get_duty_schedule_provider_registry(
    *,
    app: Optional[Flask] = None,
    registry: Optional[DutyScheduleProviderRegistry] = None,
) -> Optional[DutyScheduleProviderRegistry]:
    if registry is not None:
        return registry
    target_app = app
    if target_app is None and has_app_context():
        target_app = current_app._get_current_object()
    if target_app is None:
        return None
    value = target_app.extensions.get(EXTENSION_KEY)
    return value if isinstance(value, DutyScheduleProviderRegistry) else None


def register_duty_schedule_provider(app: Flask, provider: Any) -> None:
    registry = get_duty_schedule_provider_registry(app=app)
    if registry is None:
        registry = init_duty_schedule_provider_registry(app)
    registry.register(provider)


def unregister_duty_schedule_provider(app: Flask, provider_id: str) -> None:
    registry = get_duty_schedule_provider_registry(app=app)
    if registry is not None:
        registry.unregister(provider_id)


def get_duty_schedule_provider_status(*, registry=None) -> Dict[str, Any]:
    current = get_duty_schedule_provider_registry(registry=registry)
    return current.status() if current is not None else _missing_status()


def get_duty_schedule_provider_revision(*, registry=None) -> Dict[str, Any]:
    current = get_duty_schedule_provider_registry(registry=registry)
    return current.revision() if current is not None else _missing_status()


def get_duty_schedule_release_projection(*, registry=None) -> Dict[str, Any]:
    current = get_duty_schedule_provider_registry(registry=registry)
    return current.release_projection() if current is not None else DutyScheduleProviderRegistry().release_projection()


def get_duty_schedule_months(*, registry=None) -> Dict[str, Any]:
    current = get_duty_schedule_provider_registry(registry=registry)
    return current.months() if current is not None else {**_missing_status(), "months": []}


def get_duty_schedule_month(year: int, month: int, *, registry=None) -> Dict[str, Any]:
    current = get_duty_schedule_provider_registry(registry=registry)
    if current is None:
        return {**_missing_status(), "year": year, "month": month, "days": [], "employees": [], "shifts": []}
    return current.month(year, month)
