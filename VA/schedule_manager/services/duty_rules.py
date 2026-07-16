SHIFT_DEFINITIONS = {
    "8": {"title": "Основная смена МСК", "time": "09:00-18:00", "location": "Москва"},
    "ХД": {"title": "Основной Хабаровск Дежурный", "time": "00:00-09:00", "location": "Хабаровск"},
    "ХР": {"title": "Хабаровск Резерв", "time": "02:00-11:00", "location": "Хабаровск"},
    "ДД": {"title": "Основной Дневной дежурный МСК", "time": "08:00-17:00", "location": "Москва"},
    "ДР": {"title": "Резервный дежурный МСК", "time": "09:00-18:00", "location": "Москва"},
    "ВД": {"title": "Основной Вечерний дежурный МСК", "time": "16:00-01:00", "location": "Москва"},
    "ВР": {"title": "Резервный вечерний дежурный МСК", "time": "16:00-01:00", "location": "Москва"},
}

REQUIRED_WEEKDAY_DUTY_SHIFTS = ("ХД", "ДД", "ДР", "ВД", "ВР")
OPTIONAL_WEEKDAY_DUTY_SHIFTS = ("ХР",)
MOSCOW_DUTY_SHIFTS = ("ДД", "ДР", "ВД", "ВР")
KHABAROVSK_SHIFTS = ("ХД", "ХР")
DUTY_SHIFTS = REQUIRED_WEEKDAY_DUTY_SHIFTS + OPTIONAL_WEEKDAY_DUTY_SHIFTS
WEEKEND_CODES = {"сб", "вс"}
ABSENCE_CODES = {"отпуск", "О"}
HOLIDAY_CODES = {"Праздник", "П"}
WEEKEND_MARK = "Вых"
WEEKEND_ALLOWED_CODES = {"", "Вых", "ВХ", "О", "отпуск", "П", "Праздник"}
HOLIDAY_ALLOWED_CODES = WEEKEND_ALLOWED_CODES
HOLIDAY_WORK_CODE = "ВХ"
