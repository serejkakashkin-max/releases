from flask import Blueprint, make_response, redirect, render_template, request

from services.employee_directory_service import load_employee_directory_context
from VA.schedule_manager.integrations.employee_directory_adapter import (
    VaSettingsMigrationRequiredError,
    get_managed_va_employees,
    get_va_employee_directory_write_state,
)
from VA.schedule_manager.repositories.competency_repository import CompetencyRepository
from VA.schedule_manager.repositories.managed_employee_repository import ManagedEmployeeRepository
from VA.schedule_manager.repositories.employee_settings_repository import (
    EmployeeSettingsConflictError,
    EmployeeSettingsRepository,
    EmployeeSettingsValidationError,
)
from VA.schedule_manager.repositories.integration_settings_repository import (
    IntegrationSettingsRepository,
)
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.calendar_integration_service import (
    CalendarIntegrationService,
)
from VA.schedule_manager.services.competency_service import (
    CompetencyInUseError,
    CompetencyService,
    CompetencyValidationError,
)
from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.shift_service import (
    ShiftInUseError,
    ShiftService,
    ShiftValidationError,
)
from VA.schedule_manager.services.user_messages import build_user_messages
from VA.schedule_manager.url_helpers import public_url_for


settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


def _competency_service() -> CompetencyService:
    return CompetencyService(CompetencyRepository(), ManagedEmployeeRepository())


def _shift_service() -> ShiftService:
    return ShiftService(
        ShiftRepository(),
        schedule_service=ScheduleService(ScheduleRepository()),
    )


def _calendar_integration_service() -> CalendarIntegrationService:
    return CalendarIntegrationService(IntegrationSettingsRepository())


@settings_bp.get("/")
def index():
    return redirect(public_url_for("va_schedule_manager.settings.employees"))


@settings_bp.get("/employees")
def employees():
    context = load_employee_directory_context()
    write_state = get_va_employee_directory_write_state()
    try:
        all_employees = get_managed_va_employees(context)
        error = request.args.get("error")
    except VaSettingsMigrationRequiredError as exc:
        all_employees = []
        error = request.args.get("error") or str(exc)
    response = make_response(
        render_template(
            "va_schedule_manager/settings/employees.html",
            employees=all_employees,
            active_count=sum(item.status == "active" for item in all_employees),
            statuses=("active", "long_leave"),
            roles=("employee", "manager"),
            competencies=_competency_service().list_competencies(),
            employee_directory_write_state=write_state,
            user_messages=build_user_messages(
                message=request.args.get("message"),
                error=error,
            ),
            message=request.args.get("message"),
            error=error,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@settings_bp.post("/employees/<employee_id>/settings")
def update_employee_settings(employee_id: str):
    allowed_fields = {"status", "role", "competencies", "overtime_ready"}
    submitted_fields = {
        key
        for key in request.form
        if key
        not in {
            "settings_revision",
            "settings_etag",
            "directory_etag",
        }
    }
    if not submitted_fields.issubset(allowed_fields):
        return redirect(
            public_url_for(
                "va_schedule_manager.settings.employees",
                error="Unsupported employee settings fields.",
            )
        )
    values = {
        "status": request.form.get("status", "active"),
        "role": request.form.get("role", "employee"),
        "competencies": request.form.getlist("competencies"),
        "overtime_ready": request.form.get("overtime_ready", "0") == "1",
    }
    try:
        EmployeeSettingsRepository().save_employee_settings(
            employee_id,
            values,
            expected_revision=request.form.get("settings_revision"),
            expected_etag=request.form.get("settings_etag", ""),
            expected_directory_etag=request.form.get("directory_etag", ""),
        )
    except (EmployeeSettingsConflictError, EmployeeSettingsValidationError) as exc:
        return redirect(
            public_url_for(
                "va_schedule_manager.settings.employees",
                error=str(exc),
            )
        )
    return redirect(
        public_url_for(
            "va_schedule_manager.settings.employees",
            message="VA settings updated.",
        )
    )


@settings_bp.get("/competencies")
def competencies():
    service = _competency_service()
    all_competencies = service.list_competencies()
    delete_code = request.args.get("delete")
    delete_competency = next(
        (item for item in all_competencies if item.code == delete_code),
        None,
    )
    delete_usage = service.used_by_employees(delete_code) if delete_competency else []
    return render_template(
        "va_schedule_manager/settings/competencies.html",
        competencies=all_competencies,
        show_add_competency=request.args.get("add") == "1",
        delete_competency=delete_competency,
        delete_usage=delete_usage,
        user_messages=build_user_messages(
            message=request.args.get("message"),
            error=request.args.get("error"),
        ),
    )


@settings_bp.post("/competencies")
def add_competency():
    service = _competency_service()
    try:
        service.add_competency(request.form)
    except CompetencyValidationError as exc:
        return redirect(
            public_url_for("va_schedule_manager.settings.competencies", error=str(exc))
        )
    return redirect(
        public_url_for(
            "va_schedule_manager.settings.competencies",
            message="Competency added.",
        )
    )


@settings_bp.post("/competencies/update")
def update_competency():
    service = _competency_service()
    try:
        service.update_competency(request.form.get("original_code", ""), request.form)
    except CompetencyValidationError as exc:
        return redirect(
            public_url_for("va_schedule_manager.settings.competencies", error=str(exc))
        )
    return redirect(
        public_url_for(
            "va_schedule_manager.settings.competencies",
            message="Competency updated.",
        )
    )


@settings_bp.post("/competencies/delete")
def delete_competency():
    service = _competency_service()
    code = request.form.get("code", "")
    try:
        service.delete_competency(code)
    except (CompetencyValidationError, CompetencyInUseError) as exc:
        return redirect(
            public_url_for(
                "va_schedule_manager.settings.competencies",
                delete=code,
                error=str(exc),
            )
        )
    return redirect(
        public_url_for(
            "va_schedule_manager.settings.competencies",
            message="Competency deleted.",
        )
    )


@settings_bp.get("/shifts")
def shifts():
    service = _shift_service()
    all_shifts = service.list_shifts()
    delete_code = request.args.get("delete")
    delete_shift = next((shift for shift in all_shifts if shift.code == delete_code), None)
    delete_usage = service.find_usage(delete_code) if delete_shift else []
    return render_template(
        "va_schedule_manager/settings/shifts.html",
        shifts=all_shifts,
        show_add_shift=request.args.get("add") == "1",
        delete_shift=delete_shift,
        delete_usage=delete_usage,
        user_messages=build_user_messages(
            message=request.args.get("message"),
            error=request.args.get("error"),
        ),
    )


@settings_bp.post("/shifts")
def add_shift():
    service = _shift_service()
    try:
        service.add_shift(request.form)
    except ShiftValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.settings.shifts", error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.shifts", message="Shift added."))


@settings_bp.post("/shifts/update")
def update_shift():
    service = _shift_service()
    try:
        service.update_shift(request.form.get("original_code", ""), request.form)
    except ShiftValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.settings.shifts", error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.shifts", message="Shift updated."))


@settings_bp.post("/shifts/delete")
def delete_shift():
    service = _shift_service()
    code = request.form.get("code", "")
    try:
        service.delete_shift(code)
    except (ShiftValidationError, ShiftInUseError) as exc:
        return redirect(
            public_url_for("va_schedule_manager.settings.shifts", delete=code, error=str(exc))
        )
    return redirect(public_url_for("va_schedule_manager.settings.shifts", message="Shift deleted."))


@settings_bp.post("/shifts/reset")
def reset_shifts():
    _shift_service().reset_defaults()
    return redirect(public_url_for("va_schedule_manager.settings.shifts", message="Shifts reset."))


@settings_bp.get("/integrations")
def integrations():
    service = _calendar_integration_service()
    return render_template(
        "va_schedule_manager/settings/integrations.html",
        settings=service.get_settings(),
        user_messages=build_user_messages(
            message=request.args.get("message"),
            error=request.args.get("error"),
        ),
    )


@settings_bp.post("/integrations/calendar")
def save_calendar_integration():
    _calendar_integration_service().save_settings(request.form)
    return redirect(
        public_url_for(
            "va_schedule_manager.settings.integrations",
            message="Integration settings saved.",
        )
    )
