import html
import re
from datetime import date
from typing import Optional

from markupsafe import Markup
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, abort, redirect, render_template, request, send_file, send_from_directory
from werkzeug.security import safe_join

from VA.schedule_manager.url_helpers import public_url_for
from VA.schedule_manager.parsers.excel_parser import ExcelParseError
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.repositories.employee_repository import EmployeeRepository
from VA.schedule_manager.repositories.integration_settings_repository import IntegrationSettingsRepository
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.services.calendar_integration_service import CalendarIntegrationService
from VA.schedule_manager.services.employee_service import EmployeeService
from VA.schedule_manager.services.import_service import ImportService, UploadValidationError
from VA.schedule_manager.services.schedule_autoplan_service import (
    ScheduleAutoplanAvailability,
    ScheduleAutoplanService,
    ScheduleAutoplanValidationError,
)
from VA.schedule_manager.services.schedule_display_service import ScheduleDisplayService
from VA.schedule_manager.services.schedule_export_service import ScheduleExportError, ScheduleExportService
from VA.schedule_manager.services.schedule_month_service import (
    MONTH_NAMES,
    ScheduleMonthService,
    ScheduleMonthUsage,
    ScheduleMonthValidationError,
)
from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.shift_service import ShiftService
from VA.schedule_manager.services.today_schedule_service import TodayScheduleService
from VA.schedule_manager.services.user_messages import build_user_messages
from VA.schedule_manager.config import DOCS_DIR, EXPORT_DIR


web_bp = Blueprint("web", __name__)
DOC_FILES = [
    {
        "title": "Руководство пользователя",
        "description": "Основные сценарии работы с графиком, справочниками и загрузкой данных.",
        "path": "user-guide.md",
    },
    {
        "title": "API АС графиков",
        "description": "Список текущих API-методов, форматы запросов и ответов.",
        "path": "api.md",
    },
    {
        "title": "Источник требований",
        "description": "Зафиксированные правила из приложенного файла требований.",
        "path": "ui-and-integrations-source.md",
    },
    {
        "title": "Миграционная сборка",
        "description": "Как подключить модуль графиков к родительской АС с минимальными правками.",
        "path": "migration-build.md",
    },
    {
        "title": "Правила графиков",
        "description": "Логика формирования и проверки графиков дежурств.",
        "path": "duty-scheduling-rules.md",
    },
    {
        "title": "Журнал изменений",
        "description": "Что менялось в АС и какие документы обновлялись.",
        "path": "changelog.md",
    },
]
ALLOWED_DOC_PATHS = {item["path"] for item in DOC_FILES} | {"README.md", "source/ui-and-integrations-guide.html"}


def _doc_title(filename: str) -> str:
    for item in DOC_FILES:
        if item["path"] == filename:
            return item["title"]
    return "Документ"


def _render_markdown(text: str) -> Markup:
    html_parts = []
    paragraph = []
    in_list = False
    in_code = False
    code_lines = []

    def close_paragraph():
        nonlocal paragraph
        if paragraph:
            html_parts.append(f"<p>{_render_inline(' '.join(paragraph))}</p>")
            paragraph = []

    def close_list():
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        if line.startswith("```"):
            close_paragraph()
            close_list()
            if in_code:
                html_parts.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_lines.append(raw_line)
            continue

        if not line.strip():
            close_paragraph()
            close_list()
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            close_paragraph()
            close_list()
            level = len(heading.group(1))
            html_parts.append(f"<h{level}>{_render_inline(heading.group(2))}</h{level}>")
            continue

        if line.startswith("- "):
            close_paragraph()
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{_render_inline(line[2:])}</li>")
            continue

        close_list()
        paragraph.append(line)

    close_paragraph()
    close_list()
    if in_code:
        html_parts.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")

    return Markup("\n".join(html_parts))


def _render_inline(value: str) -> str:
    escaped = html.escape(value)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _services() -> tuple[ScheduleService, ImportService]:
    repository = ScheduleRepository()
    return ScheduleService(repository), ImportService(repository)


def _shift_service() -> ShiftService:
    return ShiftService(ShiftRepository())


def _active_employees() -> list:
    return [
        employee
        for employee in EmployeeService(EmployeeRepository()).list_employees()
        if employee.status == "active"
    ]


def _display_service(schedule_service: ScheduleService) -> ScheduleDisplayService:
    return ScheduleDisplayService(schedule_service, _shift_service())


def _today_state(schedule_service: ScheduleService) -> dict:
    return TodayScheduleService(schedule_service, _shift_service()).get_state().to_dict()


def _schedule_month_service() -> ScheduleMonthService:
    return ScheduleMonthService(
        ScheduleRepository(),
        EmployeeRepository(),
        CalendarIntegrationService(IntegrationSettingsRepository()),
    )


def _schedule_autoplan_service() -> ScheduleAutoplanService:
    return ScheduleAutoplanService(ScheduleRepository(), EmployeeRepository(), _shift_service())


def _schedule_export_service(schedule_service: ScheduleService) -> ScheduleExportService:
    return ScheduleExportService(schedule_service, _shift_service())


def _workbook_schedule_context(display_service: ScheduleDisplayService) -> dict:
    context = display_service.build_context(_int_arg("year"), _int_arg("month"))
    sheet_name = context.get("selected_sheet_name")
    availability = ScheduleAutoplanAvailability(False, "")
    if sheet_name:
        availability = _schedule_autoplan_service().availability(sheet_name)
    context["autoplan_availability"] = availability
    context["show_autoplan_confirm"] = (
        bool(sheet_name)
        and request.args.get("autoplan") == sheet_name
        and availability.can_autoplan
    )
    return context


def _create_month_context() -> dict:
    today = date.today()
    next_month = today.month + 1
    year = today.year
    if next_month > 12:
        next_month = 1
        year += 1
    return {
        "create_year_options": list(range(today.year - 1, today.year + 4)),
        "create_month_options": [{"number": number, "name": name} for number, name in MONTH_NAMES.items()],
        "default_create_year": year,
        "default_create_month": next_month,
    }


def _delete_month_usage() -> Optional[ScheduleMonthUsage]:
    sheet_name = request.args.get("delete_month")
    if not sheet_name:
        return None
    try:
        return _schedule_month_service().analyze_month(sheet_name)
    except ScheduleMonthValidationError:
        return None


def _int_arg(name: str) -> Optional[int]:
    value = request.args.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


@web_bp.get("/")
def index():
    schedule_service, _ = _services()
    display_service = _display_service(schedule_service)
    snapshot = schedule_service.get_current()
    message = request.args.get("message")
    error = request.args.get("error")
    return render_template(
        "va_schedule_manager/index.html",
        snapshot=snapshot,
        error=error,
        message=message,
        user_messages=build_user_messages(message=message, error=error),
        shift_lookup=display_service.shift_lookup(),
        shift_options=display_service.shift_options(),
        shift_options_payload=display_service.shift_options_payload(),
        active_employees=_active_employees(),
        today_state=_today_state(schedule_service),
        delete_month_usage=_delete_month_usage(),
        **_create_month_context(),
        **_workbook_schedule_context(display_service),
    )


@web_bp.post("/upload")
def upload():
    schedule_service, import_service = _services()
    display_service = _display_service(schedule_service)
    file = request.files.get("file")
    if file is None:
        return render_template(
            "va_schedule_manager/index.html",
            snapshot=schedule_service.get_current(),
            error="Файл не передан.",
            message=None,
            user_messages=build_user_messages(error="Файл не передан."),
            shift_lookup=display_service.shift_lookup(),
            shift_options=display_service.shift_options(),
            shift_options_payload=display_service.shift_options_payload(),
            active_employees=_active_employees(),
            today_state=_today_state(schedule_service),
            **_create_month_context(),
            **_workbook_schedule_context(display_service),
        ), 400

    try:
        snapshot = import_service.import_file(file)
    except (UploadValidationError, ExcelParseError) as exc:
        return render_template(
            "va_schedule_manager/index.html",
            snapshot=schedule_service.get_current(),
            error=str(exc),
            message=None,
            user_messages=build_user_messages(error=str(exc)),
            shift_lookup=display_service.shift_lookup(),
            shift_options=display_service.shift_options(),
            shift_options_payload=display_service.shift_options_payload(),
            active_employees=_active_employees(),
            today_state=_today_state(schedule_service),
            **_create_month_context(),
            **_workbook_schedule_context(display_service),
        ), 400

    return render_template(
        "va_schedule_manager/index.html",
        snapshot=snapshot,
        error=None,
        message="График загружен.",
        user_messages=build_user_messages(message="График загружен."),
        shift_lookup=display_service.shift_lookup(),
        shift_options=display_service.shift_options(),
        shift_options_payload=display_service.shift_options_payload(),
        active_employees=_active_employees(),
        today_state=_today_state(schedule_service),
        **_create_month_context(),
        **_workbook_schedule_context(display_service),
    )


@web_bp.post("/update")
def update():
    schedule_service, _ = _services()
    display_service = _display_service(schedule_service)
    if schedule_service.get_current() is None:
        return redirect(public_url_for("va_schedule_manager.web.index", message="Сначала загрузите Excel-файл."))
    return render_template(
        "va_schedule_manager/index.html",
        snapshot=schedule_service.get_current(),
        error=None,
        message="Выберите новый Excel-файл, чтобы обновить график.",
        user_messages=build_user_messages(info="Выберите новый Excel-файл, чтобы обновить график."),
        shift_lookup=display_service.shift_lookup(),
        shift_options=display_service.shift_options(),
        shift_options_payload=display_service.shift_options_payload(),
        active_employees=_active_employees(),
        today_state=_today_state(schedule_service),
        focus_upload=True,
        **_create_month_context(),
        **_workbook_schedule_context(display_service),
    )


@web_bp.post("/clear")
def clear():
    schedule_service, _ = _services()
    schedule_service.clear_current()
    return redirect(public_url_for("va_schedule_manager.web.index"))


@web_bp.post("/schedule/create-month")
def create_schedule_month():
    try:
        result = _schedule_month_service().create_month(
            year=int(request.form.get("year", 0)),
            month=int(request.form.get("month", 0)),
            employee_source=str(request.form.get("employee_source", "last_schedule")),
        )
    except (TypeError, ValueError):
        return redirect(public_url_for("va_schedule_manager.web.index", error="Некорректный месяц или год."))
    except ScheduleMonthValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.web.index", error=str(exc)))

    message = f"График {result.title} создан. Сотрудников: {result.employee_count}."
    if result.calendar_warning:
        message = f"{message} {result.calendar_warning}"
    return redirect(public_url_for("va_schedule_manager.web.index", year=result.year, month=result.month, message=message))


@web_bp.post("/schedule/delete-month")
def delete_schedule_month():
    sheet_name = str(request.form.get("sheet_name", ""))
    action = str(request.form.get("action", "delete_empty"))
    selected_year = _int_arg("year")
    selected_month = _int_arg("month")

    try:
        result = _schedule_month_service().delete_month(sheet_name, action)
    except ScheduleMonthValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.web.index", year=selected_year, month=selected_month, delete_month=sheet_name, error=str(exc)))

    if result.action == "clear_filled":
        return redirect(
            public_url_for(
                "va_schedule_manager.web.index",
                year=selected_year,
                month=selected_month,
                message=f"Из графика {result.title} очищены заполненные смены: {result.filled_cells_count}.",
            )
        )
    return redirect(public_url_for("va_schedule_manager.web.index", message=f"График {result.title} удален."))


@web_bp.post("/schedule/copy-month")
def copy_schedule_month():
    try:
        result = _schedule_month_service().copy_month(
            source_sheet_name=str(request.form.get("source_sheet_name", "")),
            target_year=int(request.form.get("target_year", 0)),
            target_month=int(request.form.get("target_month", 0)),
            overwrite=request.form.get("overwrite") == "on",
        )
    except (TypeError, ValueError):
        return redirect(public_url_for("va_schedule_manager.web.index", error="Некорректный месяц или год."))
    except ScheduleMonthValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.web.index", error=str(exc)))

    message = f"График {result.title} скопирован из {result.source_sheet_name}."
    if result.overwritten:
        message = f"{message} Целевой месяц перезаписан."
    if result.calendar_warning:
        message = (
            f"{message} {result.calendar_warning} "
            "Рекомендуется свериться с производственным календарем и вручную проставить праздничные дни."
        )
    return redirect(public_url_for("va_schedule_manager.web.index", year=result.year, month=result.month, message=message))


@web_bp.post("/schedule/autoplan")
def autoplan_schedule_month():
    sheet_name = str(request.form.get("sheet_name", ""))
    selected_year = request.form.get("year")
    selected_month = request.form.get("month")
    try:
        result = _schedule_autoplan_service().autoplan(
            sheet_name,
            vacations_confirmed=request.form.get("vacations_confirmed") == "on",
        )
    except ScheduleAutoplanValidationError as exc:
        return redirect(public_url_for("va_schedule_manager.web.index", year=selected_year, month=selected_month, autoplan=sheet_name, error=str(exc)))

    message = f"Автопланирование {result.title} выполнено. Заполнено ячеек: {result.assigned_cells_count}."
    if result.violation_count:
        message = f"{message} Остались замечания проверки: {result.violation_count}."
    return redirect(public_url_for("va_schedule_manager.web.index", year=selected_year, month=selected_month, message=message))


@web_bp.get("/schedule/export")
def export_schedule():
    schedule_service, _ = _services()
    sheet_name = str(request.args.get("sheet_name", ""))
    try:
        filename, stream = _schedule_export_service(schedule_service).export_month(sheet_name)
    except ScheduleExportError as exc:
        return redirect(public_url_for("va_schedule_manager.web.index", error=str(exc)))
    safe_name = Path(filename).name or "schedule.xlsx"
    export_name = f"{uuid4().hex}_{safe_name}"
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    export_path = EXPORT_DIR / export_name
    stream.seek(0)
    export_path.write_bytes(stream.read())
    return send_file(
        export_path,
        as_attachment=True,
        download_name=safe_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@web_bp.get("/docs")
def docs_index():
    return render_template(
        "va_schedule_manager/docs.html",
        active_page="docs",
        docs_files=DOC_FILES,
        source_html_path="source/ui-and-integrations-guide.html",
    )


@web_bp.get("/docs/file/<path:filename>")
def docs_file(filename: str):
    if filename not in ALLOWED_DOC_PATHS:
        abort(404)
    if filename.endswith(".md"):
        safe_path = safe_join(DOCS_DIR, filename)
        if not safe_path:
            abort(404)
        path = Path(safe_path)
        if not path.exists() or not path.is_file():
            abort(404)
        return render_template(
            "va_schedule_manager/doc_view.html",
            active_page="docs",
            doc_title=_doc_title(filename),
            filename=filename,
            content=_render_markdown(path.read_text(encoding="utf-8")),
        )
    return send_from_directory(DOCS_DIR, filename)


@web_bp.get("/docs/download/<path:filename>")
def docs_download(filename: str):
    if filename not in ALLOWED_DOC_PATHS:
        abort(404)
    return send_from_directory(DOCS_DIR, filename, as_attachment=True)
