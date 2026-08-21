from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class AutoplanRule:
    rule_id: str
    title: str
    level: str
    statement: str
    exception_policy: str = ""


@dataclass(frozen=True)
class AutoplanContract:
    source_data_file: str
    historical_load_months: int
    weekday_shift_codes: Tuple[str, ...]
    weekend_excluded_shift_codes: Tuple[str, ...]
    required_weekday_shift_codes: Tuple[str, ...]
    optional_weekday_shift_codes: Tuple[str, ...]
    newcomer_trainee_shift_codes: Tuple[str, ...]
    load_shift_codes: Tuple[str, ...]
    evening_shift_codes: Tuple[str, ...]
    holiday_work_code: str
    primary_shift_code: str
    rules: Tuple[AutoplanRule, ...]


AUTOPLAN_CONTRACT = AutoplanContract(
    source_data_file="schedule_data.json",
    historical_load_months=3,
    weekday_shift_codes=("ХД", "ДД", "ДР", "ВД", "ВР", "ХР"),
    weekend_excluded_shift_codes=("ХД", "ДД", "ДР", "ВД", "ВР", "ХР", "8"),
    required_weekday_shift_codes=("ХД", "ДД", "ДР", "ВД", "ВР"),
    optional_weekday_shift_codes=("ХР",),
    newcomer_trainee_shift_codes=("ДР", "ВР"),
    load_shift_codes=("ХД", "ХР", "ДД", "ДР", "ВД", "ВР", "ВХ"),
    evening_shift_codes=("ВД", "ВР"),
    holiday_work_code="ВХ",
    primary_shift_code="8",
    rules=(
        AutoplanRule(
            "source.schedule-data-only",
            "Единственный источник данных",
            "mandatory",
            "Автопланировщик работает только с сохраненными данными АС и не перечитывает Excel после импорта.",
        ),
        AutoplanRule(
            "manual.keep-existing-cells",
            "Сохранение ручных назначений",
            "mandatory",
            "Уже внесенные пользователем смены не изменяются; автопланировщик только дозаполняет свободные ячейки.",
        ),
        AutoplanRule(
            "calendar.no-weekday-shifts-on-weekends",
            "Выходные без недельных смен",
            "mandatory",
            "На выходные и праздничные дни не назначаются ХД, ДД, ДР, ВД, ВР, ХР и 8; для таких дней допустима ровно одна ВХ, праздник, отпуск или пустое значение.",
        ),
        AutoplanRule(
            "week.continue-manual-block",
            "Продолжение начатой недели",
            "mandatory",
            "Если недельный блок уже начат вручную, свободные рабочие дни этой недели дозаполняются тем же сотрудником.",
        ),
        AutoplanRule(
            "week.continue-previous-month",
            "Переходящая неделя",
            "mandatory",
            "Если месяц начинается не с понедельника, автопланировщик проверяет предыдущий сохраненный месяц и продолжает недельные смены до первого стопора; оставшиеся дни выводятся как ручное замечание.",
        ),
        AutoplanRule(
            "load.previous-three-months",
            "Нагрузка за прошлые периоды",
            "mandatory",
            "При выборе кандидатов учитывается нагрузка за три предыдущих сохраненных месяца.",
        ),
        AutoplanRule(
            "coverage.required-weekday-shifts",
            "Закрытие обязательных смен",
            "mandatory",
            "В рабочие дни закрываются ХД, ДД, ДР, ВД и ВР; ХР закрывается по возможности.",
        ),
        AutoplanRule(
            "coverage.one-holiday-work",
            "Одна ВХ на нерабочий день",
            "mandatory",
            "На каждый выходной или праздничный день назначается ровно один сотрудник в ВХ.",
        ),
        AutoplanRule(
            "employee.location",
            "Локационные ограничения",
            "mandatory",
            "Москва не назначается в ХД/ХР, Хабаровск не назначается в московские смены.",
        ),
        AutoplanRule(
            "employee.manager",
            "Руководитель",
            "mandatory",
            "Руководитель не назначается в недельные дежурные смены; в ВХ допускается только с наименьшим приоритетом.",
        ),
        AutoplanRule(
            "employee.newcomer-history",
            "Новичок",
            "mandatory",
            "Новичок сразу допускается к ДР/ВР; к остальным дежурным сменам и ВХ — только если такая смена уже была у него в графике, включая текущий месяц.",
        ),
        AutoplanRule(
            "employee.week-availability",
            "Отпуск внутри недели",
            "mandatory",
            "Если у сотрудника на неделе есть отпуск или недоступность, его нельзя назначать в новую недельную дежурную смену; переходящая смена из прошлого месяца продолжается только до первого стопора.",
        ),
        AutoplanRule(
            "fairness.single-shift-type-per-month",
            "Не повторять тип дежурства",
            "mandatory",
            "Для сотрудников один и тот же тип дежурства не повторяется больше одного раза в месяц при наличии альтернатив; для Хабаровска это означает чередование ХД/ХР между неделями.",
            "Исключение возможно только после согласования и с объяснением в артефакте автоплана.",
        ),
        AutoplanRule(
            "fairness.no-evening-two-weeks",
            "Вечерние не подряд",
            "mandatory",
            "ВД/ВР не назначаются одному сотруднику две недели подряд.",
            "Исключение возможно только после согласования и с объяснением в артефакте автоплана.",
        ),
        AutoplanRule(
            "fairness.khabarovsk-alternating-shifts",
            "Хабаровск чередует ХД/ХР",
            "mandatory",
            "ХД и ХР не назначаются одному сотруднику в тот же код две рабочие недели подряд при наличии второго доступного хабаровского сотрудника.",
            "Исключение возможно только после согласования и с объяснением в артефакте автоплана.",
        ),
        AutoplanRule(
            "mpr.evening-priority",
            "Приоритет МПР в вечерних сменах",
            "mandatory",
            "При назначении ВД/ВР приоритет отдается МПР-координатору, но два МПР-координатора не назначаются одновременно в ВД/ВР.",
        ),
        AutoplanRule(
            "mpr.only-one-available",
            "Единственный доступный МПР",
            "mandatory",
            "Если МПР-координаторов всего двое и один из них в отпуске, второго нельзя назначать в вечерние смены.",
            "Исключение возможно только после согласования и с объяснением в артефакте автоплана.",
        ),
        AutoplanRule(
            "finalize.primary-shift-validation",
            "Финализация",
            "mandatory",
            "После заполнения свободным московским сотрудникам ставится 8, часы пересчитываются, затем запускается валидатор.",
        ),
        AutoplanRule(
            "artifact.cell-level-explanations",
            "Пояснения автоплана",
            "mandatory",
            "После автоплана под графиком выводится человеческое пояснение и хинты на ячейках дежурных смен; ручная правка очищает отметку автопланировщика на измененной ячейке.",
        ),
    ),
)


def autoplan_rule_ids() -> Tuple[str, ...]:
    return tuple(rule.rule_id for rule in AUTOPLAN_CONTRACT.rules)
