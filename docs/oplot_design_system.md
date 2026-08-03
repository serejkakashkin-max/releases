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
