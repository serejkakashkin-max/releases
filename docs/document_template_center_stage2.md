# Центр шаблонов — Этап 2

Этап 2 добавляет защищённое редактирование DOCX через независимые кандидаты. Действующий файл меняется только короткой атомарной операцией после security-, contract- и synthetic-generation проверок.

## Доступность после Этапа 5Б

Отдельные DTC login, editor token, session secret и feature flag удалены. Центр доступен как часть «Блока релизов». Изменяющие формы защищены host-only HttpOnly CSRF cookie с prefix-safe Path; файловая валидация, audit, locking и atomic publish остаются обязательными.

Публичный URL Центра — `/dashboard/release-monitor/document-templates/`. После загрузки новой версии существующие validation и atomic publish выполняются как единая защищённая операция. Отдельное пользовательское подтверждение публикации не требуется; при ошибке проверки или SHA-конфликте действующий DOCX не изменяется.

## Runtime и обслуживание

Кандидаты и тестовые документы располагаются в `cache/document_template_center`, история и audit — в `data/document_template_center` относительно `RELEASE_WEB_RUNTIME_DIR`. Эти каталоги исключены из Git.

```powershell
python tools/cleanup_document_template_center.py
python tools/recover_document_template_center.py
```

Cleanup помечает истёкшие кандидаты. Recovery не повторяет `replace`: он только сопоставляет фактические SHA с persisted operation metadata и финализирует outcome. `publish_failed` блокирует дальнейшие mutations документа до контролируемого вмешательства.

## Ограничения безопасности

- upload — максимум 10 МиБ по request/file headers и фактически прочитанным байтам;
- path/filename не являются идентификаторами endpoint; используются opaque document ID и UUID;
- внешний content DOCX не загружается preview-компонентом;
- synthetic generation использует `https://example.invalid/jira` и не обращается к Jira/Confluence;
- publish/rollback automation разрешён только на temporary synthetic roots;
- первый publish/rollback реального шаблона требует отдельного разрешения, свежего backup и SHA-256 baseline.

## Отложенная проверка перед развёртыванием

Контролируемый publish/rollback одного рабочего шаблона отложен до pre-deployment проверки в окружении, максимально близком к стенду.

Перед первым включением записи необходимо выполнить:

1. backup выбранного DOCX;
2. фиксацию исходного SHA-256;
3. upload byte-for-byte candidate;
4. validation и test generation;
5. publish;
6. обычную генерацию документа;
7. rollback;
8. финальную сверку SHA-256.

До этой проверки запись должна оставаться ограниченной операционными правилами окружения.

## Проверки перед commit

Использовать только explicit staging paths, затем:

```powershell
git status --short
git diff --cached --stat
git diff --cached --name-status
git diff --cached --check
```

В staged paths не допускаются candidate/test/history DOCX, metadata, audit, rate-limit state, locks и cleanup state.
