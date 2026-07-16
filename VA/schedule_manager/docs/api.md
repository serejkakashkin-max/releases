# API АС графиков

## Общий формат ответа

Все новые API возвращают единую оболочку:

```json
{
  "ok": true,
  "data": {},
  "error": null,
  "meta": {}
}
```

Ошибка:

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "validation_error",
    "message": "Описание ошибки",
    "details": {}
  },
  "meta": {}
}
```

## Состояние загрузки

### GET `/integration/health`

Health-check для родительской АС и reverse proxy.

Пример ответа:

```json
{
  "ok": true,
  "data": {
    "status": "ok",
    "module": {
      "name": "schedule-manager",
      "title": "АС графиков дежурств",
      "version": "0.1.0-migration",
      "base_path": "",
      "public_base_url": ""
    }
  },
  "error": null,
  "meta": {}
}
```

### GET `/integration/manifest`

Возвращает паспорт модуля для родительской АС: название, версию, `base_path`, UI-входы, основные API и contract хранения.

### GET `/schedule/export`

Скачивает выбранный график в Excel.

Параметры query string:

- `sheet_name` — имя месяца/графика в данных АС.

Ответ: `.xlsx`-файл с тремя видимыми листами:

- сам график месяца;
- `К публикации`;
- `Для внесения в табель`.

### GET `/api/status`

Возвращает состояние текущего загруженного графика.

### GET `/api/check`

Совместимый alias для `/api/status`.

Пример успешного ответа:

```json
{
  "ok": true,
  "data": {
    "has_data": true,
    "uploaded_at": "2026-07-13 17:10:21",
    "employee_count": 15,
    "original_filename": "График_Июль_2026.xlsx"
  },
  "error": null,
  "meta": {}
}
```

## Состояние на сегодня

### GET `/api/today`

Возвращает основного дежурного сейчас и состав смен на текущую дату.

Пример:

```json
{
  "ok": true,
  "data": {
    "has_data": true,
    "date": "2026-07-13",
    "title": "Июль 2026",
    "primary_shift": "ВД",
    "primary_duty_employee": "Частухин А. М.",
    "shifts": [
      {
        "code": "ВД",
        "display_code": "ВД",
        "name": "МСК вечерний дежурный",
        "color": "#00B0F0",
        "text_color": "#ffffff",
        "employees": ["Частухин А. М."]
      }
    ]
  },
  "error": null,
  "meta": {}
}
```

## Сотрудники

### GET `/api/employees`

Возвращает сотрудников из текущего загруженного графика.

Если график не загружен, возвращает `404` с кодом `schedule_not_loaded`.

Карточка сотрудника содержит поля:

- `name`;
- `email`;
- `phone`;
- `status`;
- `personnel_number`;
- `role`;
- `location`;
- `competencies`;
- `overtime_ready`.

Поля `competencies`, `location` и `overtime_ready` используются справочником сотрудников и проверками графика: компетенция `manager` ограничивает сотрудника сменой `8`, компетенция `mpr_coordinator` включает правила МПР-координаторов, локация сотрудника определяет допустимость московских и хабаровских смен, а `overtime_ready` разрешает автоматическое назначение `ВХ`.

### GET `/api/competencies`

Возвращает справочник компетенций сотрудников.

Пример:

```json
{
  "ok": true,
  "data": {
    "competencies": [
      {
        "code": "manager",
        "name": "Руководитель",
        "description": "Руководитель может быть только в смене 8.",
        "is_system": true
      },
      {
        "code": "support",
        "name": "Сотрудник сопровождения",
        "description": "Исполнитель смен сопровождения.",
        "is_system": true
      },
      {
        "code": "mpr_coordinator",
        "name": "МПР-координатор",
        "description": "Компетенция для правил совместимости МПР в графике.",
        "is_system": true
      }
    ]
  },
  "error": null,
  "meta": {}
}
```

## Редактирование ячейки графика

### POST `/api/schedule/cell`

Меняет смену сотрудника в текущем месяце.

Тело запроса:

```json
{
  "sheet_name": "Июль_2026",
  "employee_name": "Иванов И.И.",
  "day": 13,
  "shift_code": "ДД"
}
```

`shift_code` можно передать пустой строкой, чтобы очистить ячейку.

Успешный ответ:

```json
{
  "ok": true,
  "data": {
    "cell": {
      "employee_name": "Иванов И.И.",
      "day": 13,
      "shift_code": "ДД",
      "display_code": "ДД",
      "shift_name": "МСК дневной дежурный",
      "color": "#92D050",
      "text_color": "#1f2933"
    },
    "row": {
      "employee_name": "Иванов И.И.",
      "hours": 160
    },
    "schedule": {
      "title": "Июль 2026",
      "violation_count": 0,
      "violations": []
    }
  },
  "error": null,
  "meta": {}
}
```

## Массовое заполнение ячеек

### POST `/api/schedule/bulk-fill`

Заполняет несколько ячеек текущего месяца одним значением и сохраняет изменения в `data/schedule_data.json`.

Тело запроса:

```json
{
  "sheet_name": "Июль 2026",
  "cells": [
    {
      "employee_name": "Иванов И.И.",
      "day": 13
    }
  ],
  "shift_code": "ДД"
}
```

`shift_code` можно передать пустой строкой, чтобы очистить выделенные ячейки.

Если передан `Праздник` или его алиас `П`, значение применяется ко всей выбранной дате для всех сотрудников текущего графика. То же правило действует для одиночного редактирования через `POST /api/schedule/cell`.

Успешный ответ:

```json
{
  "ok": true,
  "data": {
    "cells": [
      {
        "employee_name": "Иванов И.И.",
        "day": 13,
        "shift_code": "ДД",
        "display_code": "ДД",
        "shift_name": "МСК дневной дежурный",
        "color": "#92D050",
        "text_color": "#1f2933"
      }
    ],
    "rows": [
      {
        "employee_name": "Иванов И.И.",
        "hours": 168
      }
    ],
    "schedule": {
      "title": "Июль 2026",
      "violation_count": 0,
      "violations": []
    },
    "applied_to_full_days": false
  },
  "error": null,
  "meta": {}
}
```

## Добавление сотрудника в месяц

### POST `/api/schedule/employee`

Добавляет строку сотрудника в текущий месяц.

Тело запроса:

```json
{
  "sheet_name": "Июль_2026",
  "employee_name": "Иванов И.И.",
  "fill_mode": "empty"
}
```

`fill_mode`:

- `empty` — все ячейки пустые;
- `workdays_8` — смена `8` по рабочим дням.

Успешный ответ возвращает `201`.

## Удаление сотрудника из месяца

### DELETE `/api/schedule/employee`

Удаляет строку сотрудника из текущего месяца. Справочник сотрудников не меняется.

Тело запроса:

```json
{
  "sheet_name": "Июль_2026",
  "employee_name": "Иванов И.И."
}
```

Успешный ответ:

```json
{
  "ok": true,
  "data": {
    "employee_name": "Иванов И.И.",
    "schedule": {
      "title": "Июль 2026",
      "violation_count": 0,
      "violations": []
    }
  },
  "error": null,
  "meta": {}
}
```

## Создание нового месяца

### POST `/api/schedule/month`

Создает новый месячный график и сохраняет его в `data/schedule_data.json`.

Тело запроса:

```json
{
  "year": 2026,
  "month": 8,
  "employee_source": "last_schedule"
}
```

`employee_source`:

- `last_schedule` — взять список сотрудников из последнего графика;
- `directory` — взять активных сотрудников из справочника.

Успешный ответ возвращает `201`:

```json
{
  "ok": true,
  "data": {
    "schedule": {
      "sheet_name": "Август 2026",
      "title": "Август 2026",
      "year": 2026,
      "month": 8,
      "employee_count": 15
    },
    "calendar": {
      "source": "consultant",
      "warning": ""
    }
  },
  "error": null,
  "meta": {}
}
```

Если производственный календарь не настроен или недоступен, поле `calendar.warning` содержит текст для пользователя, но график создается с датами, днями недели и сотрудниками.

По умолчанию используется `КонсультантПлюс`: `https://www.consultant.ru/law/ref/calendar/proizvodstvennye/`.

Для года АС открывает страницу вида:

```text
https://www.consultant.ru/law/ref/calendar/proizvodstvennye/2027/
```

Из HTML-таблиц `table.cal` дни с классом `holiday` считаются официальными праздниками и превращаются в отметку `П` при создании нового графика.

Резервный провайдер `isDayOff API`: `https://isdayoff.ru/api/getdata`.

Запрос для месяца строится с параметрами:

- `year` — год;
- `month` — месяц;
- `cc=ru` — Россия;
- `pre=1` — учитывать предпраздничные дни;
- `holiday=1` — выделять официальные праздники отдельным кодом.

Ответ `isDayOff` — строка по дням месяца. Код `8` считается официальным праздником и превращается в отметку `П` при создании нового графика. Коды обычных выходных не превращаются в `П`.

## Удаление или очистка месяца

### DELETE `/api/schedule/month`

Удаляет пустой график месяца или очищает заполненные смены.

Тело запроса:

```json
{
  "sheet_name": "Август 2026",
  "action": "delete_empty"
}
```

`action`:

- `delete_empty` — удалить только если в графике нет заполненных смен, кроме `Праздник` и `Отпуск`;
- `clear_filled` — очистить заполненные смены, сохранив `Праздник` и `Отпуск`;
- `delete_any` — удалить месяц целиком.

Успешный ответ:

```json
{
  "ok": true,
  "data": {
    "schedule": {
      "sheet_name": "Август 2026",
      "title": "Август 2026",
      "action": "delete",
      "filled_cells_count": 0
    }
  },
  "error": null,
  "meta": {}
}
```

## Копирование месяца

### POST `/api/schedule/month/copy`

Копирует график из одного месяца в другой и сохраняет результат в `data/schedule_data.json`.

Тело запроса:

```json
{
  "source_sheet_name": "Июль 2026",
  "target_year": 2026,
  "target_month": 8,
  "overwrite": true
}
```

Если целевой месяц уже существует и `overwrite=false`, API возвращает ошибку `validation_error`.

При копировании:

- строки сотрудников копируются из источника;
- смены переносятся по номерам дней;
- лишние дни источника отбрасываются, если целевой месяц короче;
- новые дни остаются пустыми, если целевой месяц длиннее;
- праздники целевого месяца из производственного календаря проставляются как `П`.

Если производственный календарь недоступен, `calendar.warning` содержит предупреждение для пользователя.

Успешный ответ возвращает `201`:

```json
{
  "ok": true,
  "data": {
    "schedule": {
      "source_sheet_name": "Июль 2026",
      "sheet_name": "Август 2026",
      "title": "Август 2026",
      "year": 2026,
      "month": 8,
      "employee_count": 15,
      "overwritten": true
    },
    "calendar": {
      "source": "КонсультантПлюс",
      "warning": ""
    }
  },
  "error": null,
  "meta": {}
}
```

## Тестовые/демо API

### GET `/api/sample-history-analysis`

Анализирует пример истории из `sample_data/june_2026_history.csv`.

### GET `/api/sample-july-validation`

Проверяет пример июльского графика из `sample_data/july_2026_expected_weekends.csv`.

## Важное правило состояния

Excel используется только при загрузке файла. После загрузки API и интерфейс работают с `data/schedule_data.json`.
