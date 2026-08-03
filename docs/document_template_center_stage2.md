# Центр шаблонов — Этап 2

Этап 2 добавляет защищённое редактирование DOCX через независимые кандидаты. Действующий файл меняется только короткой атомарной операцией после security-, contract- и synthetic-generation проверок.

## Конфигурация

Центр по-прежнему включается только для процесса:

```powershell
$env:DOCUMENT_TEMPLATE_CENTER_ENABLED='1'
$env:DOCUMENT_TEMPLATE_EDITOR_TOKEN='<shared editor token>'
$env:SUP_ADMIN_SESSION_SECRET='<cryptographically random secret, at least 32 bytes>'
$env:SESSION_COOKIE_SECURE='1' # обязательно на production HTTPS
```

`SUP_ADMIN_SESSION_SECRET` используется как существующий application signing secret. Он не является editor access token. Слабый, отсутствующий или default secret закрывает только Центр шаблонов с безопасной ошибкой 503. Значения secret/token не должны попадать в Git, логи или URL.

Proxy headers по умолчанию не учитываются. `TRUST_PROXY_HEADERS=1` разрешается только за настроенным доверенным reverse proxy.

## Runtime и обслуживание

Кандидаты и тестовые документы располагаются в `cache/document_template_center`, история и audit — в `data/document_template_center` относительно `RELEASE_WEB_RUNTIME_DIR`. Эти каталоги исключены из Git.

```powershell
python tools/cleanup_document_template_center.py
python tools/recover_document_template_center.py
```

Cleanup помечает истёкшие кандидаты. Recovery не повторяет `replace`: он только сопоставляет фактические SHA с persisted operation metadata и финализирует outcome. `publish_failed` блокирует дальнейшие mutations документа до контролируемого вмешательства.

Emergency disable:

```powershell
$env:DOCUMENT_TEMPLATE_CENTER_ENABLED='0'
```

## Ограничения безопасности

- upload — максимум 10 МиБ по request/file headers и фактически прочитанным байтам;
- path/filename не являются идентификаторами endpoint; используются opaque document ID и UUID;
- внешний content DOCX не загружается preview-компонентом;
- synthetic generation использует `https://example.invalid/jira` и не обращается к Jira/Confluence;
- publish/rollback automation разрешён только на temporary synthetic roots;
- первый publish/rollback реального шаблона требует отдельного разрешения, свежего backup и SHA-256 baseline.

## Проверки перед commit

Использовать только explicit staging paths, затем:

```powershell
git status --short
git diff --cached --stat
git diff --cached --name-status
git diff --cached --check
```

В staged paths не допускаются candidate/test/history DOCX, metadata, audit, rate-limit state, locks и cleanup state.
