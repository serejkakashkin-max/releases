# Центр шаблонов документов — Этап 1

Этап добавляет изолированный read-only интерфейс на локальных Tabler, HTMX, JSZip и docx-preview. Upload, validation, publish, history и rollback отсутствуют. Рабочий `DOC_TEMPLATES_ROOT` и генератор документов не изменяются.

## Доступность после Этапа 5Б

Исторический feature flag Этапа 1 удалён. Центр регистрируется вместе с приложением и открывается без отдельного DTC login/token:

```powershell
\.venv\Scripts\python.exe -m flask --app app:app run --port 5001 --no-debugger --no-reload
```

Откройте `/dashboard/release-monitor/document-templates`. За reverse proxy публичные static и application URL учитывают `X-Forwarded-Prefix`, `SCRIPT_NAME` и существующие настройки `BASE_PATH`.

## Vendor assets

Проверка локальных файлов:

```powershell
\.venv\Scripts\python.exe tools\verify_vendor_assets.py
```

Зафиксированные файлы и SHA-256 перечислены в `static/vendor/manifest.json`. CDN fallback отсутствует. Если assets нужно восстановить вручную, скачайте именно следующие официальные артефакты:

- `@tabler/core` 1.4.0 npm tarball `https://registry.npmjs.org/@tabler/core/-/core-1.4.0.tgz`: `package/dist/css/tabler.min.css` и `package/dist/js/tabler.min.js`;
- HTMX 2.0.10: `https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.10/dist/htmx.min.js`;
- JSZip 3.10.1: `https://raw.githubusercontent.com/Stuk/jszip/v3.10.1/dist/jszip.min.js`;
- docx-preview 0.3.6: `https://raw.githubusercontent.com/VolodymyrBaydalka/docxjs/0.3.6/dist/docx-preview.min.js`.

После ручного восстановления SHA-256 должен в точности совпасть с manifest. Не добавляйте CDN URL в templates.

## Проверка

```powershell
\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Preview загружает DOCX read-only. Перед рендерингом браузер создаёт временную in-memory ZIP-копию, заменяет внешние изображения локальным placeholder и нейтрализует остальные external relationships. Исходный DOCX не записывается.
