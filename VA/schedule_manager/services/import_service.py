from datetime import datetime
from pathlib import Path
from uuid import uuid4

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot, grid_to_dict
from VA.schedule_manager.parsers.excel_parser import parse_employees_from_excel
from VA.schedule_manager.parsers.monthly_workbook_parser import parse_all_month_sheets
from VA.schedule_manager.repositories.employee_repository import EmployeeRepository
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.services.competency_service import COMPETENCY_SUPPORT
from VA.schedule_manager.config import ALLOWED_EXTENSIONS, MAX_UPLOAD_SIZE_BYTES, UPLOAD_DIR


class UploadValidationError(Exception):
    pass


class ImportService:
    def __init__(self, repository: ScheduleRepository, employee_repository: EmployeeRepository = None) -> None:
        self.repository = repository
        self.employee_repository = employee_repository or EmployeeRepository()

    def import_file(self, file: FileStorage) -> ScheduleSnapshot:
        original_filename = file.filename or ""
        self._validate_filename(original_filename)
        self._validate_file_size(file)

        stored_path = self._save_upload(file, original_filename)
        employees = self._with_directory_competencies(parse_employees_from_excel(stored_path))
        if not employees:
            raise UploadValidationError("На листе 'Справочник' не найдены сотрудники.")
        try:
            parsed_months = parse_all_month_sheets(stored_path)
        except Exception as exc:
            raise UploadValidationError(f"Не удалось прочитать месячные листы: {exc}") from exc
        if not parsed_months:
            raise UploadValidationError("В файле не найдены листы с месячными графиками.")

        month_schedules = []
        for option, grid in parsed_months:
            month_schedules.append(
                {
                    "year": option.year,
                    "month": option.month,
                    "month_name": option.month_name,
                    "sheet_name": option.sheet_name,
                    "label": option.label,
                    "grid": grid_to_dict(grid),
                }
            )

        snapshot = ScheduleSnapshot(
            employees=employees,
            original_filename=original_filename,
            stored_filename=stored_path.name,
            uploaded_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            month_schedules=month_schedules,
        )
        self.repository.save(snapshot)
        return snapshot

    def _with_directory_competencies(self, employees: list) -> list:
        directory = {employee.name: employee for employee in self.employee_repository.load_all()}
        result = []
        for employee in employees:
            directory_employee = directory.get(employee.name)
            if directory_employee is not None:
                result.append(directory_employee)
                continue
            result.append(
                Employee(
                    name=employee.name,
                    email=employee.email,
                    phone=employee.phone,
                    status=employee.status,
                    personnel_number=employee.personnel_number,
                    role="employee",
                    location="moscow",
                    competencies=(COMPETENCY_SUPPORT,),
                )
            )
        return result

    def _validate_filename(self, filename: str) -> None:
        if not filename:
            raise UploadValidationError("Файл не выбран.")

        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            raise UploadValidationError(f"Неверный тип файла. Разрешены: {allowed}.")

    def _save_upload(self, file: FileStorage, original_filename: str) -> Path:
        safe_name = secure_filename(original_filename) or "schedule.xlsx"
        stored_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}_{safe_name}"
        path = UPLOAD_DIR / stored_name
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        file.save(path)
        if path.stat().st_size > MAX_UPLOAD_SIZE_BYTES:
            try:
                path.unlink()
            except OSError:
                pass
            raise UploadValidationError("Файл слишком большой.")
        return path

    def _validate_file_size(self, file: FileStorage) -> None:
        if file.content_length and file.content_length > MAX_UPLOAD_SIZE_BYTES:
            raise UploadValidationError("Файл слишком большой.")
