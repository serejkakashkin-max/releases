from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleGrid, ScheduleRow
from VA.schedule_manager.services.autoplan.audit import AutoplanAuditBuilder
from VA.schedule_manager.services.autoplan.capacity import DutyCapacityDiagnostics
from VA.schedule_manager.services.autoplan.candidates import CandidateGenerator
from VA.schedule_manager.services.autoplan.context import PlanningContext
from VA.schedule_manager.services.autoplan.rules import EmployeeRuleSet


@dataclass(frozen=True)
class PlannerDependencies:
    duty_capacity: DutyCapacityDiagnostics
    audit_builder: AutoplanAuditBuilder
    candidate_generator: CandidateGenerator
    employee_rules: EmployeeRuleSet
    holiday_work_code: str
    khabarovsk_shifts: tuple
    evening_shift_codes: Set[str]
    weekend_codes: Set[str]
    week_groups: Callable[[ScheduleGrid], List[list]]
    moscow_shift_order: Callable[[Set[str]], tuple]
    is_holiday_day: Callable[[Dict[str, dict], int], bool]
    is_absence: Callable[[object], bool]
    existing_shift_workers: Callable[[Dict[str, dict], int, str], List[str]]
    existing_week_assignments: Callable[[Dict[str, dict], List[int]], Dict[str, str]]
    continued_week_assignments: Callable[[ScheduleGrid, List], Dict[str, str]]
    fill_days: Callable[[dict, List[int], str, bool], List[int]]
    fill_days_until_stop: Callable[[dict, List[int], str], tuple]
    calculate_hours: Callable[[dict], int]
    days_period: Callable[[List[int]], str]
    newcomer_reasons: Callable[[str, str], List[str]]
    evening_reasons: Callable[[str], List[str]]


class MonthPlanner:
    def __init__(self, dependencies: PlannerDependencies) -> None:
        self.dependencies = dependencies

    def plan(
        self,
        context: PlanningContext,
        previous_week_evening: Set[str],
        progress: Optional[Callable[[str], None]] = None,
    ) -> tuple[ScheduleGrid, List[dict], List[dict]]:
        deps = self.dependencies
        grid = context.grid
        employees = context.employees
        assignments = context.assignments
        counters = context.counters
        holiday_work_counters = context.holiday_work_counters
        current_month_holiday_work_counts = context.current_month_holiday_work_counts
        current_month_duty_blocks = context.current_month_duty_blocks
        monthly_shift_usage = context.monthly_shift_usage
        newcomer_shift_history = context.newcomer_shift_history
        previous_week_shift_workers: Dict[str, Set[str]] = {}
        assignment_explanations: List[dict] = []
        stop_cells: List[dict] = []

        for week_days in deps.week_groups(grid):
            period = deps.days_period([day.day for day in week_days])
            deps.duty_capacity.start_week(period)
            self._progress(progress, f"Планирую неделю {period}.")
            week_day_numbers = [day.day for day in week_days]
            nonworking_day_numbers = [
                day.day
                for day in week_days
                if day.weekday.lower() in deps.weekend_codes or deps.is_holiday_day(assignments, day.day)
            ]
            workday_numbers = [
                day.day
                for day in week_days
                if day.weekday.lower() not in deps.weekend_codes and not deps.is_holiday_day(assignments, day.day)
            ]

            unavailable = {
                employee_name
                for employee_name, row_assignments in assignments.items()
                if any(deps.is_absence(row_assignments.get(day, "")) for day in week_day_numbers)
            }
            week_assigned: Set[str] = set()

            self._plan_nonworking_days(
                context,
                nonworking_day_numbers,
                unavailable,
                assignment_explanations,
            )

            if not workday_numbers:
                deps.duty_capacity.finish_week()
                continue

            current_week_evening: Set[str] = set()
            current_week_day_primary_mpr: Set[str] = set()
            current_week_shift_workers: Dict[str, Set[str]] = {}

            existing_week_assignments = deps.existing_week_assignments(assignments, workday_numbers)
            for shift_code, employee_name in existing_week_assignments.items():
                if employee_name not in assignments:
                    continue
                filled_days = deps.fill_days(
                    assignments[employee_name],
                    workday_numbers,
                    shift_code,
                    True,
                )
                if filled_days:
                    assignment_explanations.append(
                        deps.audit_builder.assignment_explanation(
                            employee_name,
                            shift_code,
                            deps.days_period(filled_days),
                            filled_days,
                            [employee_name],
                            counters.get(employee_name, 0),
                            [
                                "смена уже была начата вручную",
                                "автопланировщик дозаполнил пустые дни этого недельного блока",
                                "существующие ручные назначения не изменялись",
                            ],
                            headline="Эту смену на неделю за вами закрепили вручную — автопланировщик только дозаполнил пустые дни недели.",
                        )
                    )
                deps.duty_capacity.note_existing_block(employee_name, shift_code)
                self._register_weekday_assignment(
                    context,
                    employee_name,
                    shift_code,
                    week_assigned,
                    current_week_shift_workers,
                    current_week_evening,
                    current_week_day_primary_mpr,
                )

            applied_continued_shift_codes = self._plan_continued_week(
                context,
                week_days,
                workday_numbers,
                week_assigned,
                current_week_shift_workers,
                current_week_evening,
                current_week_day_primary_mpr,
                existing_week_assignments,
                assignment_explanations,
                stop_cells,
                progress,
            )

            for shift_code in deps.khabarovsk_shifts:
                if shift_code in existing_week_assignments or shift_code in applied_continued_shift_codes:
                    continue
                self._plan_weekday_shift(
                    context,
                    shift_code,
                    "khabarovsk",
                    workday_numbers,
                    unavailable,
                    week_assigned,
                    previous_week_evening,
                    current_week_evening,
                    current_week_day_primary_mpr,
                    previous_week_shift_workers,
                    current_week_shift_workers,
                    assignment_explanations,
                )

            for shift_code in deps.moscow_shift_order(previous_week_evening):
                if shift_code in existing_week_assignments or shift_code in applied_continued_shift_codes:
                    continue
                self._plan_weekday_shift(
                    context,
                    shift_code,
                    "moscow",
                    workday_numbers,
                    unavailable,
                    week_assigned,
                    previous_week_evening,
                    current_week_evening,
                    current_week_day_primary_mpr,
                    previous_week_shift_workers,
                    current_week_shift_workers,
                    assignment_explanations,
                )

            self._fill_default_moscow_shift(context, workday_numbers, week_assigned, assignment_explanations)

            previous_week_evening = current_week_evening
            previous_week_shift_workers = current_week_shift_workers
            deps.duty_capacity.finish_week()

        rows = [
            ScheduleRow(row.employee_name, deps.calculate_hours(assignments[row.employee_name]), assignments[row.employee_name])
            for row in grid.employees
        ]
        return ScheduleGrid(grid.title, grid.year, grid.month, grid.days, rows), assignment_explanations, stop_cells

    def _plan_nonworking_days(
        self,
        context: PlanningContext,
        nonworking_day_numbers: List[int],
        unavailable: Set[str],
        assignment_explanations: List[dict],
    ) -> None:
        deps = self.dependencies
        assignments = context.assignments
        for nonworking_day in nonworking_day_numbers:
            existing_workers = deps.existing_shift_workers(assignments, nonworking_day, deps.holiday_work_code)
            if existing_workers:
                employee_name = existing_workers[0]
                context.counters[employee_name] = context.counters.get(employee_name, 0) + 1
                context.holiday_work_counters[employee_name] = context.holiday_work_counters.get(employee_name, 0) + 1
                context.current_month_holiday_work_counts[employee_name] = (
                    context.current_month_holiday_work_counts.get(employee_name, 0) + 1
                )
                continue
            candidates = deps.candidate_generator.holiday_candidates(
                self._holiday_request(context, unavailable)
            )
            employee_name = candidates[0] if candidates else ""
            if not employee_name:
                continue
            assignments[employee_name][nonworking_day] = deps.holiday_work_code
            assignment_explanations.append(
                deps.audit_builder.assignment_explanation(
                    employee_name,
                    deps.holiday_work_code,
                    f"{nonworking_day} число",
                    [nonworking_day],
                    candidates,
                    context.counters.get(employee_name, 0),
                    [
                        "дата является выходным или праздничным днем",
                        "на нерабочий день нужен один сотрудник в ВХ",
                        "сотрудник активен, относится к Москве, готов к сверхурочке и не в отпуске на этой неделе",
                        "Новичок выбирался в ВХ только если уже ходил в ВХ в прошлых периодах",
                        "выбран сотрудник с минимальным количеством ВХ в текущем месяце и за три предыдущих месяца",
                        "руководитель допускается к ВХ только с наименьшим приоритетом",
                    ],
                    headline="Из тех, кто готов выходить сверхурочно, у вас меньше всего выходов в выходные за этот и три предыдущих месяца.",
                )
            )
            context.counters[employee_name] += 1
            context.holiday_work_counters[employee_name] += 1
            context.current_month_holiday_work_counts[employee_name] += 1

    def _plan_continued_week(
        self,
        context: PlanningContext,
        week_days: List,
        workday_numbers: List[int],
        week_assigned: Set[str],
        current_week_shift_workers: Dict[str, Set[str]],
        current_week_evening: Set[str],
        current_week_day_primary_mpr: Set[str],
        existing_week_assignments: Dict[str, str],
        assignment_explanations: List[dict],
        stop_cells: List[dict],
        progress: Optional[Callable[[str], None]],
    ) -> Set[str]:
        deps = self.dependencies
        assignments = context.assignments
        applied_continued_shift_codes: Set[str] = set()
        continued_assignments = deps.continued_week_assignments(context.grid, week_days)
        for shift_code, employee_name in continued_assignments.items():
            if shift_code in existing_week_assignments:
                continue
            if employee_name not in assignments or employee_name in week_assigned:
                continue
            filled_days, stopper_day, unresolved_days = deps.fill_days_until_stop(
                assignments[employee_name],
                workday_numbers,
                shift_code,
            )
            if filled_days:
                assignment_explanations.append(
                    deps.audit_builder.assignment_explanation(
                        employee_name,
                        shift_code,
                        deps.days_period(filled_days),
                        filled_days,
                        [employee_name],
                        context.counters.get(employee_name, 0),
                        [
                            "недельная смена началась в предыдущем месяце",
                            "сотрудник продолжает тот же недельный блок в новом месяце до первого стопора",
                            "автопланировщик не меняет исполнителя с 1 числа внутри продолжающейся недели",
                        ],
                        headline="Смена началась у вас в конце прошлого месяца — вы доводите её до конца недели, исполнителя посреди недели не меняют.",
                    )
                )
            if stopper_day is not None:
                stopper_value = assignments[employee_name].get(stopper_day, "")
                for unresolved_day in unresolved_days:
                    stop_cells.append(
                        {
                            "employee_name": employee_name,
                            "day": unresolved_day,
                            "shift_code": shift_code,
                            "stopper_day": stopper_day,
                            "stopper_value": stopper_value,
                            "message": (
                                f"{shift_code}: переходящая смена остановлена "
                                f"{stopper_day} числа ({stopper_value}). "
                                f"Дни {deps.days_period(unresolved_days)} нужно распределить вручную."
                            ),
                        }
                    )
                assignment_explanations.append(
                    deps.audit_builder.assignment_explanation(
                        employee_name,
                        shift_code,
                        deps.days_period(unresolved_days),
                        [],
                        [employee_name],
                        context.counters.get(employee_name, 0),
                        [
                            "недельная смена началась в предыдущем месяце",
                            f"с {stopper_day} числа найден стопор: {stopper_value}",
                            f"дни {deps.days_period(unresolved_days)} нужно распределить вручную для смены {shift_code}",
                        ],
                        headline=f"Смена переходит с прошлой недели, но с {stopper_day} числа в графике уже стоит {stopper_value} — дни {deps.days_period(unresolved_days)} нужно распределить вручную.",
                    )
                )
                self._progress(
                    progress,
                    f"{shift_code}: {employee_name} продолжен до стопора; дни {deps.days_period(unresolved_days)} требуют ручного распределения.",
                )
            if filled_days:
                context.counters[employee_name] += 1
            applied_continued_shift_codes.add(shift_code)
            if filled_days:
                deps.duty_capacity.note_existing_block(employee_name, shift_code)
                self._register_weekday_assignment(
                    context,
                    employee_name,
                    shift_code,
                    week_assigned,
                    current_week_shift_workers,
                    current_week_evening,
                    current_week_day_primary_mpr,
                    increment_counter=False,
                )
        return applied_continued_shift_codes

    def _plan_weekday_shift(
        self,
        context: PlanningContext,
        shift_code: str,
        location: str,
        workday_numbers: List[int],
        unavailable: Set[str],
        week_assigned: Set[str],
        previous_week_evening: Set[str],
        current_week_evening: Set[str],
        current_week_day_primary_mpr: Set[str],
        previous_week_shift_workers: Dict[str, Set[str]],
        current_week_shift_workers: Dict[str, Set[str]],
        assignment_explanations: List[dict],
    ) -> None:
        deps = self.dependencies
        candidates = deps.candidate_generator.weekday_candidates(
            self._weekday_request(
                context,
                unavailable,
                week_assigned,
                location,
                shift_code,
                previous_week_evening,
                current_week_evening,
                current_week_day_primary_mpr,
                previous_week_shift_workers,
            )
        )
        employee_name = candidates[0] if candidates else ""
        if not employee_name:
            return
        deps.duty_capacity.note_shift_candidates(shift_code, candidates)
        filled_days = deps.fill_days(
            context.assignments[employee_name],
            workday_numbers,
            shift_code,
            True,
        )
        location_text = "Хабаровску" if location == "khabarovsk" else "Москве"
        reasons = [
            f"смена относится к {location_text}",
            f"сотрудник активен и относится к локации {'Хабаровск' if location == 'khabarovsk' else 'Москва'}",
            "на неделе у сотрудника нет отпуска",
            "сотрудник еще не назначен в другую дежурную смену этой недели",
            "приоритет отдавался сотрудникам, которые еще не были в этой смене в текущем месяце",
        ]
        if location == "moscow":
            reasons.insert(3, "сотрудник не является руководителем")
            reasons.extend(deps.evening_reasons(shift_code))
        if location == "khabarovsk":
            reasons.append("для Хабаровска тот же код смены не повторялся две рабочие недели подряд при наличии альтернатив")
        reasons.extend(deps.newcomer_reasons(shift_code, location))
        reasons.extend(
            [
                "выбран сотрудник с учетом суммарной нагрузки за три предыдущих месяца и планируемый месяц",
                "при наличии альтернатив автопланировщик старается не давать больше двух дежурных блоков в планируемом месяце",
            ]
        )
        assignment_explanations.append(
            deps.audit_builder.assignment_explanation(
                employee_name,
                shift_code,
                deps.days_period(filled_days),
                filled_days,
                candidates,
                context.counters.get(employee_name, 0),
                reasons,
                headline=f"Смену на неделю распределяли между {len(candidates)} подходящими сотрудниками — у вас одна из самых низких нагрузок за этот и три предыдущих месяца.",
            )
        )
        deps.duty_capacity.note_planned_assignment(
            employee_name,
            shift_code,
            candidates,
            context.current_month_duty_blocks.get(employee_name, 0),
            context.counters.get(employee_name, 0),
        )
        self._register_weekday_assignment(
            context,
            employee_name,
            shift_code,
            week_assigned,
            current_week_shift_workers,
            current_week_evening,
            current_week_day_primary_mpr,
        )

    def _fill_default_moscow_shift(
        self,
        context: PlanningContext,
        workday_numbers: List[int],
        week_assigned: Set[str],
        assignment_explanations: List[dict],
    ) -> None:
        deps = self.dependencies
        for row in context.grid.employees:
            employee = context.employees.get(row.employee_name)
            if employee is None or employee.location != "moscow":
                continue
            if row.employee_name in week_assigned:
                continue
            filled_days = []
            for day in workday_numbers:
                if not context.assignments[row.employee_name].get(day):
                    context.assignments[row.employee_name][day] = "8"
                    filled_days.append(day)
            if filled_days:
                assignment_explanations.append(
                    deps.audit_builder.assignment_explanation(
                        row.employee_name,
                        "8",
                        deps.days_period(filled_days),
                        filled_days,
                        [row.employee_name],
                        context.counters.get(row.employee_name, 0),
                        [
                            "сотрудник активен и относится к Москве",
                            "сотрудник не занят дежурной сменой на этой неделе",
                            "основная смена 8 используется для свободных рабочих дней",
                        ],
                        headline="Дежурств на эту неделю у вас нет — стоит основная смена.",
                    )
                )

    def _register_weekday_assignment(
        self,
        context: PlanningContext,
        employee_name: str,
        shift_code: str,
        week_assigned: Set[str],
        current_week_shift_workers: Dict[str, Set[str]],
        current_week_evening: Set[str],
        current_week_day_primary_mpr: Set[str],
        increment_counter: bool = True,
    ) -> None:
        deps = self.dependencies
        if increment_counter:
            context.counters[employee_name] = context.counters.get(employee_name, 0) + 1
        context.current_month_duty_blocks[employee_name] = context.current_month_duty_blocks.get(employee_name, 0) + 1
        week_assigned.add(employee_name)
        context.monthly_shift_usage.setdefault(employee_name, set()).add(shift_code)
        current_week_shift_workers.setdefault(shift_code, set()).add(employee_name)
        if shift_code in deps.evening_shift_codes:
            current_week_evening.add(employee_name)
        if deps.employee_rules.is_day_primary_mpr(employee_name, shift_code, context.employees):
            current_week_day_primary_mpr.add(employee_name)

    def _weekday_request(
        self,
        context: PlanningContext,
        unavailable: Set[str],
        week_assigned: Set[str],
        location: str,
        shift_code: str,
        previous_week_evening: Set[str],
        current_week_evening: Set[str],
        current_week_day_primary_mpr: Set[str],
        previous_week_shift_workers: Dict[str, Set[str]],
    ):
        from VA.schedule_manager.services.autoplan.candidates import WeekdayCandidateRequest

        return WeekdayCandidateRequest(
            grid=context.grid,
            employees=context.employees,
            counters=context.counters,
            unavailable=unavailable,
            week_assigned=week_assigned,
            location=location,
            allow_manager=False,
            shift_code=shift_code,
            monthly_shift_usage=context.monthly_shift_usage,
            previous_week_evening=previous_week_evening,
            current_week_evening=current_week_evening,
            current_week_day_primary_mpr=current_week_day_primary_mpr,
            current_month_duty_blocks=context.current_month_duty_blocks,
            newcomer_shift_history=context.newcomer_shift_history,
            previous_week_shift_workers=previous_week_shift_workers,
        )

    def _holiday_request(self, context: PlanningContext, unavailable: Set[str]):
        from VA.schedule_manager.services.autoplan.candidates import HolidayCandidateRequest

        return HolidayCandidateRequest(
            grid=context.grid,
            employees=context.employees,
            holiday_work_counters=context.holiday_work_counters,
            current_month_holiday_work_counts=context.current_month_holiday_work_counts,
            unavailable=unavailable,
            newcomer_shift_history=context.newcomer_shift_history,
        )

    def _progress(self, progress: Optional[Callable[[str], None]], message: str) -> None:
        if progress is not None:
            progress(message)
