# Миграционная сборка для родительской АС

Цель сборки: подключить текущую АС графиков дежурств к родительской АС с минимальными правками и без смешивания бизнес-логики графиков с внешним приложением.

## Публичная граница

- UI-вход: `/`
- Настройки: `/settings/...`
- Документация: `/docs`
- API: `/api/...`
- Паспорт модуля: `/integration/manifest`
- Health-check: `/integration/health`

Все JSON API возвращают оболочку `ok/data/error/meta`.

## Подключение под префиксом

Если родительская АС монтирует модуль не в корень, задайте:

```bash
SCHEDULE_MANAGER_BASE_PATH=/duty-schedule
```

Для WSGI-подключения:

```python
from app import create_app
from app.wsgi import mount_wsgi_app

application = mount_wsgi_app(create_app(), "/duty-schedule")
```

При таком варианте паспорт модуля будет отдавать prefixed-ссылки:

- `/duty-schedule/`
- `/duty-schedule/api/status`
- `/duty-schedule/integration/health`

## Сборка архива

```bash
scripts/build_migration_bundle.sh
```

Скрипт создаёт архив в `dist/` и исключает:

- `venv`;
- `.git`;
- `uploads`;
- `data/backups`;
- рабочие данные `data/employees.json`, `data/schedule_data.json`, `data/schedule_edits.json`;
- настройки внешних интеграций `data/integration_settings.json`;
- временные Python-кэши;
- предыдущие сборки `dist`.

## Важные ограничения

- После импорта Excel источником данных остается `data/schedule_data.json`; Excel дальше не перечитывается.
- Локальные JSON-файлы подходят для миграционной сборки и пилота, но для многопроцессной промышленной установки нужен общий store родительской АС.
- Auth/RBAC/CSRF не добавлены внутри модуля: их должна поставить родительская АС или общий gateway.
- Секреты интеграций не должны попадать в HTML, JS, логи и архив с кодом.

## Проверка после подключения

1. Открыть `/integration/health` и получить `status=ok`.
2. Открыть `/integration/manifest` и сверить `base_path`.
3. Открыть UI-вход из `manifest.ui.entrypoint`.
4. Проверить `/api/status`, `/api/today`.
5. Создать или отредактировать тестовый график и убедиться, что изменения сохраняются в состоянии АС.
