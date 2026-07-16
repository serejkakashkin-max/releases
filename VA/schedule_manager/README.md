# Schedule Manager

Изолированный VA-модуль для работы с графиками дежурств.

## Происхождение

- Источник переноса: `C:\schedule-manager-migration-20260714_163642`
- Дата интеграции: 2026-07-16
- Владелец модуля: Антон Васькин / VA
- Точка входа в основном приложении: `/admin/va/schedule-manager`

## Как подключается

Модуль регистрируется опционально через основной Flask app. Флаг:

```json
{
  "modules": {
    "va_schedule_manager": {
      "enabled": true
    }
  }
}
```

Если папка `VA/schedule_manager` отсутствует или флаг выключен, основной проект продолжает запускаться.

## Административный доступ

Доступ закрыт backend-проверкой admin-session. Для входа нужен `sup_admin_token`, а для безопасной session должен быть задан один из параметров:

- environment: `SUP_ADMIN_SESSION_SECRET`
- `config.json`: `sup_admin_session_secret`

Token не хранится в session. Для изменяющих запросов используется CSRF заголовок `X-CSRF-Token`.

## Runtime-данные

Рабочие данные не хранятся внутри `VA/`.

```text
cache/va_schedule_manager/
├── data/
├── uploads/
├── exports/
└── state/
```

Locks, backups и migration reports находятся в `cache/va_schedule_manager/state/`.

## Дополнительные зависимости

Установить только если модуль включается:

```powershell
python -m pip install -r VA/schedule_manager/requirements-va.txt
```

Отсутствие этих зависимостей не должно ломать основной Flask, если модуль выключен или папка удалена.

## Миграция данных

Миграция выполняется вручную:

```powershell
python -m VA.schedule_manager.tools.migrate_runtime_data --source "C:\schedule-manager-migration-20260714_163642" --dry-run
python -m VA.schedule_manager.tools.migrate_runtime_data --source "C:\schedule-manager-migration-20260714_163642"
```

Без `--overwrite` существующие runtime-файлы не заменяются.

## Удаление

Чтобы удалить модуль:

1. Выключить `modules.va_schedule_manager.enabled`.
2. Перезапустить Flask.
3. Удалить папку `VA/schedule_manager`.

Основной проект не должен импортировать VA-модуль на уровне корневых файлов, кроме optional registrar.
