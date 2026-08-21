# AI Context: Schedule Manager In Releases

Эта папка нужна как точка входа для отдельных AI-чатов по проекту.

## Главные правила

- Репозиторий GitHub: `https://github.com/serejkakashkin-max/releases`
- Локальный клон: `/Users/antonvaskin/Documents/Codex/2026-07-11/new-chat/work/schedule-manager-github`
- Ничего не отправлять в GitHub без прямого разрешения Антона.
- В локальном клоне `origin` настроен так, что `fetch` работает, а `push` заблокирован через push-url `DISABLED_BY_CODEX_DO_NOT_PUSH_WITHOUT_USER_APPROVAL`.
- Старый standalone-проект оставлен для сверки: `/Users/antonvaskin/Documents/Codex/2026-07-11/new-chat/work/schedule_manager`

## Где находится Schedule Manager

Schedule Manager встроен в родительскую АС как VA-модуль:

```text
VA/schedule_manager/
```

Основная точка входа в родительском приложении:

```text
/admin/va/schedule-manager
```

Статика и шаблоны модуля находятся в:

```text
VA/schedule_manager/static/va_schedule_manager/
VA/schedule_manager/templates/va_schedule_manager/
```

## Локальный dev-запуск

Окружение создано на Python 3.12:

```text
.venv/
```

Установлены зависимости из `requirements.txt` и `pytest`.

Для тестов:

```bash
.venv/bin/python -m pytest
```

Последний полный прогон после клонирования:

```text
174 passed
```

Для локального запуска нужны runtime-предпосылки, которые не коммитятся:

```text
config.json
doc_templates/
```

Они созданы локально только для разработки.

## Как дробить будущие чаты

Рекомендуемые отдельные темы:

- `VA Schedule Manager: автопланировщик`
- `VA Schedule Manager: правила проверки графика`
- `VA Schedule Manager: UI графика`
- `VA Schedule Manager: справочники сотрудников, смен, компетенций`
- `VA Schedule Manager: импорт/экспорт Excel`
- `VA Schedule Manager: интеграция с родительской АС`
- `VA Schedule Manager: API и документация`
- `VA Schedule Manager: перенос правок из standalone`

В начале нового чата достаточно дать путь к этому файлу и назвать тему.

## Текущий следующий шаг

Перед новой разработкой нужно аккуратно сравнить последние правки standalone-проекта с `VA/schedule_manager` в GitHub-клоне и переносить только то, чего еще нет в командной версии.
