from flask import Blueprint, make_response, redirect, render_template, request

from VA.schedule_manager.url_helpers import public_url_for
from VA.schedule_manager.integrations.employee_directory_adapter import (
    is_va_employee_directory_managed,
)
from VA.schedule_manager.integrations.employee_directory_commands import (
    VaEmployeeDirectoryCommandError,
    disable_va_employee_in_directory,
    ensure_va_employee_in_directory,
    get_va_employee_directory_write_state,
    update_va_employee_in_directory,
)

from VA.schedule_manager.repositories.competency_repository import CompetencyRepository
from VA.schedule_manager.repositories.employee_repository import EmployeeRepository
from VA.schedule_manager.repositories.integration_settings_repository import IntegrationSettingsRepository
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.calendar_integration_service import CalendarIntegrationService
from VA.schedule_manager.services.competency_service import (
    CompetencyInUseError,
    CompetencyService,
    CompetencyValidationError,
)
from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.employee_service import (
    EMPLOYEE_LOCATIONS,
    EMPLOYEE_STATUSES,
    EmployeeInUseError,
    EmployeeService,
    EmployeeValidationError,
)
from VA.schedule_manager.services.shift_service import ShiftInUseError, ShiftService, ShiftValidationError
from VA.schedule_manager.services.user_messages import build_user_messages
from services.employee_directory_repository import (
    EmployeeDirectoryConflictError,
    EmployeeDirectoryError,
)


settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

OVERTIME_READY_OPTIONS = {
    "1": "Да",
    "0": "Нет",
}


def _employee_service() -> EmployeeService:
    return EmployeeService(EmployeeRepository(), schedule_service=ScheduleService(ScheduleRepository()))


def _legacy_employee_service() -> EmployeeService:
    return EmployeeService(
        EmployeeRepository(directory_projection=False),
        schedule_service=ScheduleService(ScheduleRepository()),
    )


def _competency_service() -> CompetencyService:
    return CompetencyService(CompetencyRepository(), EmployeeRepository())


def _shift_service() -> ShiftService:
    return ShiftService(ShiftRepository(), schedule_service=ScheduleService(ScheduleRepository()))


def _calendar_integration_service() -> CalendarIntegrationService:
    return CalendarIntegrationService(IntegrationSettingsRepository())


def _update_employee_from_request(service: EmployeeService) -> None:
    original_name = request.form.get("original_name", "")
    name = request.form.get("name", "")
    email = request.form.get("email", "")
    phone = request.form.get("phone", "")
    location = request.form.get("location", "moscow")
    status = request.form.get("status", "active")
    competencies = request.form.getlist("competencies")
    overtime_ready = request.form.get("overtime_ready", "1") == "1"
    if not is_va_employee_directory_managed():
        service.update_employee(
            original_name,
            name,
            email,
            phone,
            status,
            location,
            competencies,
            overtime_ready,
        )
        return

    # Validate the complete VA record before changing either storage.
    service._build_employee(
        name,
        email,
        phone,
        status,
        location,
        competencies,
        overtime_ready,
    )
    legacy_service = _legacy_employee_service()
    legacy_employees = legacy_service.list_employees()
    if any(
        employee.name == name and employee.name != original_name
        for employee in legacy_employees
    ):
        raise EmployeeValidationError("Сотрудник с таким ФИО уже есть.")

    directory_field = request.form.get("directory_field", "all")
    directory_fields = {
        "va_name": ("va_name",),
        "email": ("email",),
        "phone": ("phone",),
        "location": ("location",),
        "none": (),
        "all": ("va_name", "email", "phone", "location"),
    }.get(directory_field, ())
    if directory_fields:
        update_va_employee_in_directory(
            original_name=original_name,
            full_name=name,
            email=email,
            phone=phone,
            location=location,
            expected_revision=request.form.get("directory_revision"),
            expected_etag=request.form.get("directory_etag", ""),
            fields=directory_fields,
        )
    if any(employee.name == original_name for employee in legacy_employees):
        legacy_service.update_employee(
            original_name,
            name,
            email,
            phone,
            status,
            location,
            competencies,
            overtime_ready,
        )
    elif any(employee.name == name for employee in legacy_employees):
        legacy_service.update_employee(
            name,
            name,
            email,
            phone,
            status,
            location,
            competencies,
            overtime_ready,
        )
    else:
        legacy_service.add_employee(
            name,
            email,
            phone,
            status,
            location,
            competencies,
            overtime_ready,
        )


def _disable_va_directory_membership(name: str) -> None:
    disable_va_employee_in_directory(
        name=name,
        expected_revision=request.form.get("directory_revision"),
        expected_etag=request.form.get("directory_etag", ""),
    )


def _employee_update_error(exc: Exception):
    if isinstance(exc, EmployeeDirectoryConflictError):
        message = "Центральный справочник изменился. Обновите страницу и повторите действие."
    else:
        message = str(exc)
    return redirect(public_url_for("va_schedule_manager.settings.employees", error=message))


def _legacy_employee_exists(service: EmployeeService, name: str) -> bool:
    return any(employee.name == name for employee in service.list_employees())


def _update_legacy_employee_from_form(service: EmployeeService, original_name: str) -> None:
    service.update_employee(
        original_name,
        request.form.get("name", ""),
        request.form.get("email", ""),
        request.form.get("phone", ""),
        request.form.get("status", "active"),
        request.form.get("location", "moscow"),
        request.form.getlist("competencies"),
        request.form.get("overtime_ready", "1") == "1",
    )


@settings_bp.get("/")
def index():
    return redirect(public_url_for("va_schedule_manager.settings.employees"))


@settings_bp.get("/employees")
def employees():
    service = _employee_service()
    all_employees = service.list_employees()
    directory_managed = is_va_employee_directory_managed()
    directory_write_state = get_va_employee_directory_write_state() if directory_managed else {}
    delete_name = request.args.get("delete")
    delete_employee = next((employee for employee in all_employees if employee.name == delete_name), None)
    delete_usage = service.find_schedule_usage(delete_name) if delete_employee else []
    response = make_response(render_template(
        "va_schedule_manager/settings/employees.html",
        employees=all_employees,
        active_count=service.active_count(),
        statuses=EMPLOYEE_STATUSES,
        locations=EMPLOYEE_LOCATIONS,
        overtime_ready_options=OVERTIME_READY_OPTIONS,
        competencies=_competency_service().list_competencies(),
        show_add_employee=(
            request.args.get("add") == "1"
            and (
                not directory_managed
                or bool(directory_write_state.get("writable"))
            )
        ),
        employee_directory_managed=directory_managed,
        employee_directory_write_state=directory_write_state,
        delete_employee=delete_employee,
        delete_usage=delete_usage,
        user_messages=build_user_messages(
            message=request.args.get("message"),
            error=request.args.get("error"),
        ),
        message=request.args.get("message"),
        error=request.args.get("error"),
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


@settings_bp.post("/employees")
def add_employee():
    if is_va_employee_directory_managed():
        service = _legacy_employee_service()
        name = " ".join(request.form.get("name", "").strip().split())
        try:
            if any(employee.name == name for employee in _employee_service().list_employees()):
                raise EmployeeValidationError("Сотрудник с таким ФИО уже есть.")
            directory_result = ensure_va_employee_in_directory(
                full_name=name,
                email=request.form.get("email", ""),
                phone=request.form.get("phone", ""),
                location=request.form.get("location", "moscow"),
                expected_revision=request.form.get("directory_revision"),
                expected_etag=request.form.get("directory_etag", ""),
            )
            employee_name = directory_result["employee_name"]
            if not directory_result["created"]:
                update_va_employee_in_directory(
                    original_name=employee_name,
                    full_name=directory_result["central_full_name"],
                    email=request.form.get("email", ""),
                    phone=request.form.get("phone", ""),
                    location=request.form.get("location", "moscow"),
                    expected_revision=directory_result["revision"],
                    expected_etag=directory_result["etag"],
                    fields=("email", "phone", "location"),
                )
            if _legacy_employee_exists(service, employee_name):
                _update_legacy_employee_from_form(service, employee_name)
            else:
                service.add_employee(
                    employee_name,
                    request.form.get("email", ""),
                    request.form.get("phone", ""),
                    request.form.get("status", "active"),
                    request.form.get("location", "moscow"),
                    request.form.getlist("competencies"),
                    request.form.get("overtime_ready", "1") == "1",
                )
        except EmployeeDirectoryConflictError:
            return redirect(
                public_url_for(
                    "va_schedule_manager.settings.employees",
                    error="Центральный справочник изменился. Обновите страницу и повторите добавление.",
                )
            )
        except (EmployeeDirectoryError, VaEmployeeDirectoryCommandError, EmployeeValidationError) as exc:
            return redirect(
                public_url_for(
                    "va_schedule_manager.settings.employees",
                    error=str(exc),
                )
            )
        return redirect(
            public_url_for(
                "va_schedule_manager.settings.employees",
                message="Сотрудник добавлен в центральный справочник с участием только в VA.",
            )
        )
    service = _employee_service()
    try:
        service.add_employee(
            request.form.get("name", ""),
            request.form.get("email", ""),
            request.form.get("phone", ""),
            request.form.get("status", "active"),
            request.form.get("location", "moscow"),
            request.form.getlist("competencies"),
            request.form.get("overtime_ready", "1") == "1",
        )
    except EmployeeValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.settings.employees", error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.employees", message="Сотрудник добавлен."))


@settings_bp.post("/employees/update")
def update_employee():
    service = _employee_service()
    try:
        _update_employee_from_request(service)
    except (
        EmployeeDirectoryConflictError,
        EmployeeDirectoryError,
        VaEmployeeDirectoryCommandError,
        EmployeeValidationError,
    ) as exc:
        return _employee_update_error(exc)
    return redirect(public_url_for("va_schedule_manager.settings.employees", message="Сотрудник обновлен."))


@settings_bp.get("/competencies")
def competencies():
    service = _competency_service()
    all_competencies = service.list_competencies()
    delete_code = request.args.get("delete")
    delete_competency = next((item for item in all_competencies if item.code == delete_code), None)
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
        return redirect(public_url_for("va_schedule_manager.settings.competencies", error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.competencies", message="Компетенция добавлена."))


@settings_bp.post("/competencies/update")
def update_competency():
    service = _competency_service()
    try:
        service.update_competency(request.form.get("original_code", ""), request.form)
    except CompetencyValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.settings.competencies", error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.competencies", message="Компетенция обновлена."))


@settings_bp.post("/competencies/delete")
def delete_competency():
    service = _competency_service()
    code = request.form.get("code", "")
    try:
        service.delete_competency(code)
    except (CompetencyValidationError, CompetencyInUseError) as exc:
        return redirect(public_url_for("va_schedule_manager.settings.competencies", delete=code, error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.competencies", message="Компетенция удалена."))


@settings_bp.post("/employees/status")
def change_status():
    service = _employee_service()
    try:
        service.change_status(request.form.get("name", ""), request.form.get("status", "active"))
    except EmployeeValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.settings.employees", error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.employees", message="Статус обновлен."))


@settings_bp.post("/employees/quick-update")
def quick_update_employee():
    service = _employee_service()
    try:
        _update_employee_from_request(service)
    except (
        EmployeeDirectoryConflictError,
        EmployeeDirectoryError,
        VaEmployeeDirectoryCommandError,
        EmployeeValidationError,
    ) as exc:
        return _employee_update_error(exc)
    return redirect(public_url_for("va_schedule_manager.settings.employees", message="Сотрудник обновлен."))


@settings_bp.post("/employees/delete")
def delete_employee():
    directory_managed = is_va_employee_directory_managed()
    service = _legacy_employee_service() if directory_managed else _employee_service()
    name = request.form.get("name", "")
    try:
        if directory_managed:
            usage = _employee_service().find_schedule_usage(name)
            if usage:
                raise EmployeeInUseError("Сотрудник найден в графиках. Выберите, что сделать дальше.")
            _disable_va_directory_membership(name)
            if _legacy_employee_exists(service, name):
                service.delete_employee(name)
        else:
            service.delete_employee(name)
    except (
        EmployeeDirectoryConflictError,
        EmployeeDirectoryError,
        VaEmployeeDirectoryCommandError,
        EmployeeValidationError,
        EmployeeInUseError,
    ) as exc:
        if isinstance(exc, EmployeeDirectoryConflictError):
            return _employee_update_error(exc)
        return redirect(public_url_for("va_schedule_manager.settings.employees", delete=name, error=str(exc)))
    message = "Сотрудник исключен из VA." if directory_managed else "Сотрудник удален."
    return redirect(public_url_for("va_schedule_manager.settings.employees", message=message))


@settings_bp.post("/employees/delete-from-schedules")
def delete_employee_from_schedules():
    directory_managed = is_va_employee_directory_managed()
    service = _legacy_employee_service() if directory_managed else _employee_service()
    name = request.form.get("name", "")
    try:
        output_path = service.delete_employee_with_schedule_cleanup(name)
        if directory_managed:
            _disable_va_directory_membership(name)
    except (
        EmployeeDirectoryConflictError,
        EmployeeDirectoryError,
        VaEmployeeDirectoryCommandError,
        EmployeeValidationError,
        EmployeeInUseError,
    ) as exc:
        if isinstance(exc, EmployeeDirectoryConflictError):
            return _employee_update_error(exc)
        return redirect(public_url_for("va_schedule_manager.settings.employees", delete=name, error=str(exc)))
    return redirect(
        public_url_for(
            "va_schedule_manager.settings.employees",
            message=f"Сотрудник удален из справочника. Копия графика сохранена: {output_path}",
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
    return redirect(public_url_for("va_schedule_manager.settings.shifts", message="Смена добавлена."))


@settings_bp.post("/shifts/update")
def update_shift():
    service = _shift_service()
    try:
        service.update_shift(request.form.get("original_code", ""), request.form)
    except ShiftValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.settings.shifts", error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.shifts", message="Смена обновлена."))


@settings_bp.post("/shifts/delete")
def delete_shift():
    service = _shift_service()
    code = request.form.get("code", "")
    try:
        service.delete_shift(code)
    except (ShiftValidationError, ShiftInUseError) as exc:
        return redirect(public_url_for("va_schedule_manager.settings.shifts", delete=code, error=str(exc)))
    return redirect(public_url_for("va_schedule_manager.settings.shifts", message="Смена удалена."))


@settings_bp.post("/shifts/reset")
def reset_shifts():
    service = _shift_service()
    service.reset_defaults()
    return redirect(public_url_for("va_schedule_manager.settings.shifts", message="Настройки смен сброшены к заводским."))


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
    service = _calendar_integration_service()
    service.save_settings(request.form)
    return redirect(public_url_for("va_schedule_manager.settings.integrations", message="Настройки интеграции сохранены."))
