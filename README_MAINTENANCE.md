# Режим технического обслуживания

Режим обслуживания управляется через файл `feature_flags.json`. Изменения
подхватываются без перезапуска Flask.

## Закрытие разделов

Нужный флаг меняется с `false` на `true`:

```json
{
  "maintenance": {
    "index": false,
    "release_monitor": true,
    "duty_dashboard": false,
    "chatbot": false
  }
}
```

Доступные разделы:

- `index` — главная страница;
- `release_monitor` — Блок релизов;
- `duty_dashboard` — Рабочий стол дежурного;
- `chatbot` — чат-бот.

Чтобы снова открыть раздел для всех, соответствующий флаг нужно вернуть в
`false`.

## Доступ разработчика к закрытой странице

Для личного обхода заглушки допишите к адресу страницы:

```text
?maintenance_bypass=1
```

Пример для Блока релизов:

```text
/releases/release-monitor?maintenance_bypass=1
```

Если приложение развернуто без префикса `/releases`:

```text
/release-monitor?maintenance_bypass=1
```

Bypass сохраняется в `localStorage` отдельно для каждого раздела. После первого
открытия параметр удалится из адресной строки, а страница останется доступной в
этом браузере.

Чтобы удалить личный bypass и снова увидеть заглушку:

```text
?maintenance_bypass=0
```

Для старых ссылок Блока релизов также поддерживаются параметры:

```text
?release_maintenance_bypass=1
?release_maintenance_bypass=0
```

## Обход обслуживания чат-бота

У чат-бота отдельный bypass:

```text
/?chatbot_maintenance_bypass=1
```

Для сброса:

```text
/?chatbot_maintenance_bypass=0
```

Bypass действует только в текущем браузере и не открывает раздел другим
пользователям.
