from flask import Blueprint, redirect, render_template, request

from VA.schedule_manager.repositories.integration_settings_repository import (
    IntegrationSettingsRepository,
)
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.calendar_integration_service import (
    CalendarIntegrationService,
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
    return redirect(
        public_url_for(
            "sup_parameters.sup_parameters_page",
            tab="employees",
            view="employees",
        )
    )


@settings_bp.get("/competencies")
def competencies():
    return redirect(
        public_url_for(
            "sup_parameters.sup_parameters_page",
            tab="employees",
            view="competencies",
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
