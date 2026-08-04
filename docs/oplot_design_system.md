# Oplot application shell и базовая дизайн-система

Этап 3 вводит opt-in application shell для новых и постепенно мигрируемых страниц. Legacy templates продолжают работать автономно до отдельного этапа миграции.

## Layout contract

Страница наследует `layouts/oplot_base.html` и задаёт:

- `oplot_page_title`, `oplot_page_pretitle`, `oplot_page_description`;
- `oplot_breadcrumbs` как список `{label, endpoint?, values?}`;
- optional `oplot_context_label`, `oplot_context_title`, `oplot_actor`;
- `oplot_shell_mode = 'auth'` для полноэкранного входа без sidebar/topbar.

Доступные blocks: `title`, `styles`, `page_actions`, `content`, `overlays`, `vendor_scripts`, `scripts`, `body_class`, `auth_content`. Общий layout загружает только Tabler, theme bootstrap, `oplot.css` и `oplot.js`. HTMX, JSZip, docx-preview и module assets подключаются страницей-владельцем.

## Navigation

Модель находится в `services.oplot_ui_service`. Каждый пункт имеет стабильный ID, endpoint, icon name, группу, active patterns и optional feature flag/predicate. URL всегда строится через `public_url_for`; отсутствующий endpoint безопасно скрывается.

Legacy-разделы открываются обычной полной навигацией. Sidebar не является разрешением доступа: auth и feature guards остаются в соответствующих blueprints.

## Tokens и компоненты

Общие tokens используют prefix `--oplot-`: spacing 4/8/12/16/24/32, surfaces, semantic colors, borders, radii, shadows и focus ring. Общие selectors используют prefix `oplot-`, чтобы не влиять на legacy Bootstrap templates.

`components/oplot_ui.html` предоставляет фиксированные inline icons, alert, badge, empty/error/loading states, modal shell и confirmation dialog. Компоненты должны получать пользовательский текст через autoescape и не содержать business-specific URL или scripts.

## JavaScript contract

`oplot-theme.js` выполняется до CSS, использует только `localStorage.theme` и синхронно устанавливает `data-theme` и `data-bs-theme`. `oplot.js` экспортирует:

- `window.initOplotComponents(container)`;
- `window.OplotUI.beginOperation()` / `endOperation()`;
- `window.OplotUI.showToast(message, kind)`.

Module initializers добавляются в `window.OplotComponentInitializers` и обязаны быть idempotent. После HTMX swap общий initializer вызывается для target. Module-specific status handling остаётся в module JS.

## Accessibility и визуальная проверка

Shell ориентирован на desktop 1536×864 и 1920×1080. Обязательны keyboard focus, `aria-current`, landmarks, возвращение focus после modal, перенос длинных русских названий, отсутствие horizontal overflow и поддержка `prefers-reduced-motion`. Новые assets не должны обращаться к внешним origins.

## Core pages и landing actions

Главная и справка используют общий layout contract и подключают собственные CSS/JS только через blocks `styles` и `scripts`. Module JavaScript является progressive enhancement: основные ссылки, содержание и native anchors работают без него.

Карточки главной описываются immutable landing registry в `services.oplot_ui_service`. Landing metadata содержит только navigation ID, описание, секцию, визуальный приоритет и optional refresh endpoint. Label, icon, основной endpoint, feature flag и availability всегда разрешаются через центральную navigation model; дублировать эти поля в route или template запрещено.

### Navigation invariant

Миграция интерфейса сохраняет существующие названия, порядок и иерархию пользовательского меню. Новые глобальные пункты не создаются без отдельного требования. Document Template Center относится к разделу «Блок релизов» и не является самостоятельным home action; его встраивание во внутреннее меню раздела выполняется отдельным этапом.

### Главная как визуальный эталон

Принятая главная задаёт палитру, типографику, поверхности, скругления, тени и свечение, стиль кнопок и иконок, light/dark темы, hover/focus состояния и визуальную плотность Oplot. Остальные модули наследуют этот визуальный язык, но не копируют layout главной механически: сохраняются их структура, иерархия, порядок элементов, русские названия, бизнес-функциональность и route/API/POST/redirect contracts.

Названия и порядок меню являются пользовательским контрактом; новые глобальные пункты добавляются только по отдельному требованию. Document Template Center относится к «Блоку релизов», не является пунктом главной и будет фактически встроен при миграции этого раздела.

### Контракт Блока релизов

Release Monitor использует общие визуальные primitives Oplot, а `oplot_release.css` отвечает только за page-specific layout и адаптацию существующего интерфейса. Presentation migration сохраняет структуру, точные русские названия и порядок внутреннего меню, а также route/API/POST, polling, optimistic-update и integration contracts. Центр шаблонов является условным четвёртым пунктом внутреннего меню «Блока релизов», а не самостоятельным пунктом глобальной навигации.
# Этап 5А.2: core topbar и JavaScript Блока релизов

Главная и Блок релизов используют opt-in вариант `core`: одинаковую рабочую высоту верхней полосы, бренд OPLOT и единый переключатель темы. Состав действий остаётся контекстным: статистика, версия и песочница принадлежат только главной. Default shell, sidebar, auth и Центр шаблонов не наследуют этот вариант автоматически.

Business JavaScript Блока релизов подключается локальным classic deferred-файлом. Server data и application URL передаются через один JSON config; статические URL строятся из Flask endpoints, динамические сегменты — из server-rendered templates с точным placeholder. Inline handlers считаются совместимым публичным контрактом и экспортируются явно, а ошибочная конфигурация блокирует polling и mutation requests.
