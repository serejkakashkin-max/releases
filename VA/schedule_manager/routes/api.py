from flask import Blueprint, request

from VA.schedule_manager.parsers.schedule_csv_parser import parse_schedule_csv_file
from VA.schedule_manager.repositories.competency_repository import CompetencyRepository
from VA.schedule_manager.repositories.employee_repository import EmployeeRepository
from VA.schedule_manager.repositories.integration_settings_repository import IntegrationSettingsRepository
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.routes.api_responses import api_error, api_success
from VA.schedule_manager.services.calendar_integration_service import CalendarIntegrationService
from VA.schedule_manager.services.competency_service import CompetencyService
from VA.schedule_manager.services.schedule_edit_service import ScheduleEditService, ScheduleEditValidationError
from VA.schedule_manager.services.schedule_month_service import ScheduleMonthService, ScheduleMonthValidationError
from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.schedule_validator import validate_schedule
from VA.schedule_manager.services.employee_service import EmployeeService
from VA.schedule_manager.services.shift_service import ShiftService
from VA.schedule_manager.services.today_schedule_service import TodayScheduleService
from VA.schedule_manager.services.workload_analyzer import analyze_schedule
from VA.schedule_manager.config import ENABLE_SAMPLE_ENDPOINTS, SAMPLE_DATA_DIR


api_bp = Blueprint("api", __name__)


@api_bp.errorhandler(Exception)
def handle_unexpected_error(error):
    return api_error(
        "internal_error",
        "Не удалось выполнить API-запрос.",
        500,
    )


def _schedule_service() -> ScheduleService:
    return ScheduleService(ScheduleRepository())


def _schedule_edit_service() -> ScheduleEditService:
    return ScheduleEditService(
        _schedule_service(),
        ShiftService(ShiftRepository()),
        employee_service=EmployeeService(EmployeeRepository()),
    )


def _today_schedule_service() -> TodayScheduleService:
    return TodayScheduleService(_schedule_service(), ShiftService(ShiftRepository()))


def _schedule_month_service() -> ScheduleMonthService:
    return ScheduleMonthService(
        ScheduleRepository(),
        EmployeeRepository(),
        CalendarIntegrationService(IntegrationSettingsRepository()),
    )


def _competency_service() -> CompetencyService:
    return CompetencyService(CompetencyRepository(), EmployeeRepository())


@api_bp.get("/status")
def status():
    return api_success(_status_payload())


@api_bp.get("/check")
def check():
    return api_success(_status_payload())


@api_bp.get("/today")
def today():
    return api_success(_today_schedule_service().get_state().to_dict())


def _status_payload() -> dict:
    snapshot = _schedule_service().get_current()
    if snapshot is None:
        return {"has_data": False}

    return {
        "has_data": True,
        "uploaded_at": snapshot.uploaded_at,
        "employee_count": snapshot.employee_count,
        "original_filename": snapshot.original_filename,
    }


@api_bp.get("/employees")
def employees():
    snapshot = _schedule_service().get_current()
    if snapshot is None:
        return api_error("schedule_not_loaded", "Данные не загружены.", 404)

    return api_success(
        {
            "employees": [employee.to_dict() for employee in snapshot.employees],
            "count": snapshot.employee_count,
            "uploaded_at": snapshot.uploaded_at,
        }
    )


@api_bp.get("/competencies")
def competencies():
    return api_success(
        {
            "competencies": [competency.to_dict() for competency in _competency_service().list_competencies()],
        }
    )


@api_bp.post("/schedule/cell")
def update_schedule_cell():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return api_error("invalid_json", "Ожидался JSON-объект.", 400)

    try:
        result = _schedule_edit_service().update_cell(
            sheet_name=str(payload.get("sheet_name", "")),
            employee_name=str(payload.get("employee_name", "")),
            day=int(payload.get("day", 0)),
            shift_code=str(payload.get("shift_code", "")),
        )
    except (TypeError, ValueError):
        return api_error("invalid_request", "Некорректные параметры ячейки.", 400)
    except ScheduleEditValidationError as exc:
        return api_error("validation_error", str(exc), 400)

    return api_success(
        {
            "cell": {
                "employee_name": result.employee_name,
                "day": result.day,
                "shift_code": result.shift_code,
                "display_code": result.display_code,
                "shift_name": result.shift_name,
                "color": result.color,
                "text_color": result.text_color,
            },
            "row": {
                "employee_name": result.employee_name,
                "hours": result.hours,
            },
            "schedule": {
                "title": result.title,
                "violation_count": result.violation_count,
                "violations": result.violations,
            },
            "autoplan_artifact_cleared": result.autoplan_artifact_cleared,
        }
    )


@api_bp.post("/schedule/bulk-fill")
def bulk_fill_schedule_cells():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return api_error("invalid_json", "Ожидался JSON-объект.", 400)

    cells = payload.get("cells", [])
    if not isinstance(cells, list):
        return api_error("invalid_request", "Ожидался список ячеек.", 400)

    try:
        result = _schedule_edit_service().bulk_fill(
            sheet_name=str(payload.get("sheet_name", "")),
            cells=cells,
            shift_code=str(payload.get("shift_code", "")),
        )
    except ScheduleEditValidationError as exc:
        return api_error("validation_error", str(exc), 400)

    return api_success(
        {
            "cells": result.cells,
            "rows": result.rows,
            "schedule": {
                "title": result.title,
                "violation_count": result.violation_count,
                "violations": result.violations,
            },
            "applied_to_full_days": result.applied_to_full_days,
            "autoplan_artifact_cleared": result.autoplan_artifact_cleared,
        }
    )


@api_bp.post("/schedule/employee")
def add_schedule_employee():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return api_error("invalid_json", "Ожидался JSON-объект.", 400)

    try:
        result = _schedule_edit_service().add_employee(
            sheet_name=str(payload.get("sheet_name", "")),
            employee_name=str(payload.get("employee_name", "")),
            fill_mode=str(payload.get("fill_mode", "empty")),
        )
    except ScheduleEditValidationError as exc:
        return api_error("validation_error", str(exc), 400)

    return api_success(
        {
            "row": {
                "employee_name": result.employee_name,
                "hours": result.hours,
                "assignments": {str(day): code for day, code in result.assignments.items()},
            },
            "schedule": {
                "title": result.title,
                "violation_count": result.violation_count,
                "violations": result.violations,
            },
            "autoplan_artifact_cleared": result.autoplan_artifact_cleared,
        },
        status=201,
    )


@api_bp.delete("/schedule/employee")
def delete_schedule_employee():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return api_error("invalid_json", "Ожидался JSON-объект.", 400)

    try:
        result = _schedule_edit_service().delete_employee(
            sheet_name=str(payload.get("sheet_name", "")),
            employee_name=str(payload.get("employee_name", "")),
        )
    except ScheduleEditValidationError as exc:
        return api_error("validation_error", str(exc), 400)

    return api_success(
        {
            "employee_name": result.employee_name,
            "schedule": {
                "title": result.title,
                "violation_count": result.violation_count,
                "violations": result.violations,
            },
            "autoplan_artifact_cleared": result.autoplan_artifact_cleared,
        }
    )


@api_bp.post("/schedule/month")
def create_schedule_month():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return api_error("invalid_json", "Ожидался JSON-объект.", 400)

    try:
        result = _schedule_month_service().create_month(
            year=int(payload.get("year", 0)),
            month=int(payload.get("month", 0)),
            employee_source=str(payload.get("employee_source", "last_schedule")),
        )
    except (TypeError, ValueError):
        return api_error("invalid_request", "Некорректный месяц или год.", 400)
    except ScheduleMonthValidationError as exc:
        return api_error("validation_error", str(exc), 400)

    return api_success(
        {
            "schedule": {
                "sheet_name": result.sheet_name,
                "title": result.title,
                "year": result.year,
                "month": result.month,
                "employee_count": result.employee_count,
            },
            "calendar": {
                "source": result.calendar_source,
                "warning": result.calendar_warning,
            },
        },
        status=201,
    )


@api_bp.delete("/schedule/month")
def delete_schedule_month():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return api_error("invalid_json", "Ожидался JSON-объект.", 400)

    try:
        result = _schedule_month_service().delete_month(
            sheet_name=str(payload.get("sheet_name", "")),
            action=str(payload.get("action", "delete_empty")),
        )
    except ScheduleMonthValidationError as exc:
        return api_error("validation_error", str(exc), 400)

    return api_success(
        {
            "schedule": {
                "sheet_name": result.sheet_name,
                "title": result.title,
                "action": result.action,
                "filled_cells_count": result.filled_cells_count,
            }
        }
    )


@api_bp.post("/schedule/month/copy")
def copy_schedule_month():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return api_error("invalid_json", "Ожидался JSON-объект.", 400)

    try:
        result = _schedule_month_service().copy_month(
            source_sheet_name=str(payload.get("source_sheet_name", "")),
            target_year=int(payload.get("target_year", 0)),
            target_month=int(payload.get("target_month", 0)),
            overwrite=bool(payload.get("overwrite", False)),
        )
    except (TypeError, ValueError):
        return api_error("invalid_request", "Некорректный месяц или год.", 400)
    except ScheduleMonthValidationError as exc:
        return api_error("validation_error", str(exc), 400)

    return api_success(
        {
            "schedule": {
                "source_sheet_name": result.source_sheet_name,
                "sheet_name": result.sheet_name,
                "title": result.title,
                "year": result.year,
                "month": result.month,
                "employee_count": result.employee_count,
                "overwritten": result.overwritten,
            },
            "calendar": {
                "source": result.calendar_source,
                "warning": result.calendar_warning,
            },
        },
        status=201,
    )


@api_bp.get("/sample-history-analysis")
def sample_history_analysis():
    if not ENABLE_SAMPLE_ENDPOINTS:
        return api_error("not_found", "Ресурс недоступен.", 404)
    grid = parse_schedule_csv_file(SAMPLE_DATA_DIR / "june_2026_history.csv")
    analysis = analyze_schedule(grid)
    return api_success(
        {
            "title": analysis.title,
            "employee_count": analysis.employee_count,
            "last_week_block": analysis.last_week_block,
            "workloads": [
                {
                    "employee_name": workload.employee_name,
                    "total_duty_assignments": workload.total_duty_assignments,
                    "shift_counts": workload.shift_counts,
                    "last_duty_shift": workload.last_duty_shift,
                    "hours": workload.hours,
                }
                for workload in analysis.workloads
            ],
        }
    )


@api_bp.get("/sample-july-validation")
def sample_july_validation():
    if not ENABLE_SAMPLE_ENDPOINTS:
        return api_error("not_found", "Ресурс недоступен.", 404)
    grid = parse_schedule_csv_file(SAMPLE_DATA_DIR / "july_2026_expected_weekends.csv")
    violations = validate_schedule(grid)
    return api_success(
        {
            "title": grid.title,
            "violation_count": len(violations),
            "violations": [
                {
                    "day": violation.day,
                    "shift": violation.shift,
                    "employee_name": violation.employee_name,
                    "message": violation.message,
                }
                for violation in violations
            ],
        }
    )
