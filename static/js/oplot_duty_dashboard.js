(function () {
    'use strict';

    let initializationState = 'not_started';

    function readDashboardConfig() {
        const node = document.getElementById('oplot-duty-dashboard-config');
        if (!node) throw new Error('missing_config');
        const parsed = JSON.parse(node.textContent || '{}');
        const required = ['refresh', 'approval_check', 'approval_cache_clear', 'hidden_tasks', 'hidden_task_restore', 'hidden_tasks_restore_all'];
        if (!parsed || typeof parsed !== 'object' || !parsed.urls || !parsed.data) throw new Error('invalid_config');
        required.forEach((key) => {
            const value = parsed.urls[key];
            const url = new URL(value, window.location.origin);
            if (!value || url.origin !== window.location.origin || !url.pathname.startsWith('/')) throw new Error('invalid_url');
        });
        return parsed;
    }

    function failDashboardConfig() {
        initializationState = 'failed';
        const error = document.getElementById('dutyDashboardConfigError');
        const root = document.querySelector('.oplot-duty-dashboard-root');
        if (error) error.hidden = false;
        if (root) root.inert = true;
    }

    function initOplotDutyDashboard() {
        if (initializationState !== 'not_started') return initializationState === 'initialized';
        let dashboardConfig;
        try {
            dashboardConfig = readDashboardConfig();
        } catch (_error) {
            failDashboardConfig();
            return false;
        }
        initializationState = 'initialized';
        const getDashboardUrl = (key) => dashboardConfig.urls[key];
        window.dashboardData = dashboardConfig.data.dashboard || {};
        let trashReturnFocus = null;
                let currentReleaseYearFilter = String(new Date().getFullYear());
                let currentReleaseViewFilter = 'all';
                let isReleaseMonitorExpanded = false;
                let releaseMonitorItems = [];
                let releaseMonitorSummary = {};
                let releaseMonitorMeta = {};

                function escapeHtml(value) {
                    return String(value || '')
                        .replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;')
                        .replace(/'/g, '&#39;');
                }

                function getReleaseNameHtml(item) {
                    const lines = Array.isArray(item.release_name_lines) ? item.release_name_lines : [];
                    const statusBadge = item.release_status
                        ? `<div class="release-micro-meta"><span class="release-badge ${item.is_final ? 'state-planned' : item.is_cancelled ? 'state-overdue' : item.is_overdue ? 'state-overdue' : item.is_today ? 'state-today' : 'state-planned'}">${escapeHtml(item.release_status)}</span>${item.is_pre_final ? '<span class="release-badge pre-final">Установка на ПРОМ</span>' : ''}</div>`
                        : '';

                    if (!lines.length) {
                        return statusBadge || '<span style="color: var(--text-muted);">Не указано</span>';
                    }

                    return `
                        <div class="release-name-cell">
                            ${lines.map(line => `<div class="release-name-line">${escapeHtml(line)}</div>`).join('')}
                            ${statusBadge}
                        </div>
                    `;
                }

                function buildReleaseLinkCell(url, key) {
                    if (!key) {
                        return '<span style="color: var(--text-muted);">-</span>';
                    }

                    if (!url) {
                        return escapeHtml(key);
                    }

                    return `<a href="${escapeHtml(url)}" target="_blank" class="release-key-link">${escapeHtml(key)}</a>`;
                }

                function buildReleaseRow(item) {
                    return `
                        <tr class="release-row state-${escapeHtml(item.row_state || 'planned')} ${item.has_rollout_notes ? 'has-rollout-notes' : ''}"
                            data-year="${escapeHtml(item.year)}"
                            data-status="${escapeHtml(item.release_status || '')}">
                            <td><strong>${escapeHtml(item.release_number || '')}</strong></td>
                            <td>${getReleaseNameHtml(item)}</td>
                            <td>${item.zni_key ? escapeHtml(item.zni_key) : '<span style="color: var(--text-muted);">-</span>'}</td>
                            <td>${item.ke ? escapeHtml(item.ke) : '<span style="color: var(--text-muted);">-</span>'}</td>
                            <td>${buildReleaseLinkCell(item.release_url, item.release_key)}</td>
                            <td>${buildReleaseLinkCell(item.rov_url, item.rov_key)}</td>
                            <td>${item.deployment_start ? escapeHtml(item.deployment_start) : '<span style="color: var(--text-muted);">-</span>'}</td>
                            <td>${item.deployment_end ? escapeHtml(item.deployment_end) : '<span style="color: var(--text-muted);">-</span>'}</td>
                            <td>${item.psi_owner ? escapeHtml(item.psi_owner) : '<span style="color: var(--text-muted);">-</span>'}</td>
                        </tr>
                    `;
                }

                function getSelectedReleaseYear() {
                    const select = document.getElementById('releaseYearFilter');
                    return String(select?.value || currentReleaseYearFilter || releaseMonitorMeta?.current_year || new Date().getFullYear());
                }

                function getYearFilteredReleaseItems(items) {
                    const selectedYear = getSelectedReleaseYear();
                    return (items || []).filter(item => String(item.year) === selectedYear);
                }

                function buildReleaseYearSummary(items) {
                    const summary = {
                        total: items.length,
                        non_final: 0,
                        overdue: 0,
                        today: 0,
                        pre_final: 0,
                        final: 0,
                        cancelled: 0,
                        by_status: {}
                    };

                    items.forEach(item => {
                        const statusName = item.release_status || 'Не указан';
                        summary.by_status[statusName] = (summary.by_status[statusName] || 0) + 1;

                        if (item.is_non_final) summary.non_final += 1;
                        if (item.is_overdue) summary.overdue += 1;
                        if (item.is_today) summary.today += 1;
                        if (item.is_pre_final) summary.pre_final += 1;
                        if (item.is_final) summary.final += 1;
                        if (item.is_cancelled) summary.cancelled += 1;
                    });

                    summary.by_status = Object.fromEntries(
                        Object.entries(summary.by_status).sort((a, b) => {
                            if (b[1] !== a[1]) return b[1] - a[1];
                            return a[0].localeCompare(b[0], 'ru');
                        })
                    );

                    return summary;
                }

                function updateReleaseStatusFilterOptions(items) {
                    const statusSelect = document.getElementById('releaseStatusFilter');
                    if (!statusSelect) {
                        return;
                    }

                    const currentValue = statusSelect.value || 'all';
                    const summary = buildReleaseYearSummary(items || []);
                    const statusEntries = Object.entries(summary.by_status || {});
                    statusSelect.innerHTML = '<option value="all">Все статусы</option>' +
                        statusEntries.map(([statusName, statusCount]) => (
                            `<option value="${escapeHtml(statusName)}">${escapeHtml(statusName)} (${escapeHtml(statusCount)})</option>`
                        )).join('');
                    statusSelect.value = statusEntries.some(([statusName]) => statusName === currentValue) ? currentValue : 'all';
                }

                function updateReleaseEmptyState(meta, state) {
                    const emptyState = document.getElementById('releaseMonitorEmptyState');
                    if (!emptyState) {
                        return;
                    }

                    if (state === 'initial') {
                        emptyState.innerHTML = `
                            <i class="bi bi-cloud-arrow-down"></i>
                            <div>Данные по релизам загружаются отдельно. Нажмите "Обновить релизы", чтобы получить актуальную таблицу из Jira.</div>
                        `;
                        return;
                    }

                    if (state === 'empty-year') {
                        emptyState.innerHTML = `
                            <i class="bi bi-calendar-x"></i>
                            <div>За выбранный год релизы не найдены.</div>
                        `;
                        return;
                    }

                    if (state === 'empty-filter') {
                        emptyState.innerHTML = `
                            <i class="bi bi-funnel"></i>
                            <div>По выбранным фильтрам релизы не найдены.</div>
                        `;
                        return;
                    }

                    emptyState.innerHTML = `
                        <i class="bi bi-check-circle-fill text-success"></i>
                        <div>Таблица релизов загружена, но строки для отображения отсутствуют.</div>
                    `;
                }

                function renderReleaseMonitor(items, summary, meta) {
                    const tableWrap = document.getElementById('releaseTableWrap');
                    const emptyState = document.getElementById('releaseMonitorEmptyState');
                    const lastUpdated = document.getElementById('releaseMonitorLastUpdated');
                    const yearSelect = document.getElementById('releaseYearFilter');

                    releaseMonitorItems = Array.isArray(items) ? items : [];
                    releaseMonitorSummary = summary || {};
                    releaseMonitorMeta = meta || {};

                    if (yearSelect) {
                        const years = Array.isArray(meta?.years) && meta.years.length ? meta.years : [meta?.current_year || new Date().getFullYear()];
                        const previousValue = currentReleaseYearFilter || String(meta?.current_year || years[0]);
                        yearSelect.innerHTML = years.map(year => `<option value="${escapeHtml(year)}">${escapeHtml(year)}</option>`).join('');
                        currentReleaseYearFilter = years.map(String).includes(String(previousValue))
                            ? String(previousValue)
                            : String(meta?.current_year || years[0]);
                        yearSelect.value = currentReleaseYearFilter;
                    }

                    const viewSelect = document.getElementById('releaseViewFilter');
                    if (viewSelect) {
                        currentReleaseViewFilter = viewSelect.value || 'all';
                    }

                    if (document.getElementById('releaseSummaryTotal')) {
                        const yearItems = getYearFilteredReleaseItems(releaseMonitorItems);
                        const yearSummary = buildReleaseYearSummary(yearItems);
                        document.getElementById('releaseSummaryTotal').textContent = yearSummary.total || 0;
                        document.getElementById('releaseSummaryOverdue').textContent = yearSummary.overdue || 0;
                        document.getElementById('releaseSummaryNonFinal').textContent = yearSummary.non_final || 0;
                        document.getElementById('releaseSummaryPreFinal').textContent = yearSummary.pre_final || 0;
                        updateReleaseStatusFilterOptions(yearItems);
                    }

                    if (lastUpdated) {
                        lastUpdated.textContent = meta?.last_updated || 'еще не загружалось';
                    }

                    if (tableWrap) {
                        tableWrap.classList.toggle('compact', !isReleaseMonitorExpanded);
                    }
                    if (emptyState && !releaseMonitorItems.length) {
                        emptyState.style.display = '';
                        updateReleaseEmptyState(meta, 'initial');
                    }

                    applyReleaseFilters();
                }

                function handleReleaseYearFilter() {
                    const select = document.getElementById('releaseYearFilter');
                    currentReleaseYearFilter = String(select?.value || releaseMonitorMeta?.current_year || new Date().getFullYear());
                    const yearItems = getYearFilteredReleaseItems(releaseMonitorItems);
                    updateReleaseStatusFilterOptions(yearItems);
                    applyReleaseFilters();
                }

                function handleReleaseViewFilter() {
                    const select = document.getElementById('releaseViewFilter');
                    currentReleaseViewFilter = select?.value || 'all';
                    applyReleaseFilters();
                }

                function handleReleaseSearch() {
                    applyReleaseFilters();
                }

                function toggleReleaseMonitorExpand() {
                    isReleaseMonitorExpanded = !isReleaseMonitorExpanded;
                    const tableWrap = document.getElementById('releaseTableWrap');
                    const toggleBtn = document.getElementById('releaseMonitorToggleBtn');

                    if (tableWrap) {
                        tableWrap.classList.toggle('compact', !isReleaseMonitorExpanded);
                    }

                    if (toggleBtn) {
                        toggleBtn.innerHTML = isReleaseMonitorExpanded
                            ? '<i class="bi bi-arrows-collapse"></i> Свернуть'
                            : '<i class="bi bi-arrows-expand"></i> Показать все';
                    }
                }

                function toggleReleaseMonitorSection() {
                    const section = document.getElementById('releaseMonitorSection');
                    if (section) {
                        section.classList.toggle('collapsed');
                    }
                }

                function applyReleaseFilters() {
                    const body = document.getElementById('releaseMonitorBody');
                    const tableWrap = document.getElementById('releaseTableWrap');
                    const emptyState = document.getElementById('releaseMonitorEmptyState');
                    const visibleCounter = document.getElementById('releaseVisibleCount');
                    const searchValue = (document.getElementById('releaseSearchInput')?.value || '').toLowerCase().trim();
                    const statusValue = document.getElementById('releaseStatusFilter')?.value || 'all';
                    const yearItems = getYearFilteredReleaseItems(releaseMonitorItems);
                    const filteredItems = yearItems.filter(item => {
                        const matchesView = (
                            currentReleaseViewFilter === 'all' ||
                            (currentReleaseViewFilter === 'non_final' && item.is_non_final) ||
                            (currentReleaseViewFilter === 'overdue' && item.is_overdue) ||
                            (currentReleaseViewFilter === 'today' && item.is_today) ||
                            (currentReleaseViewFilter === 'final' && item.is_final) ||
                            (currentReleaseViewFilter === 'cancelled' && item.is_cancelled)
                        );

                        const matchesStatus = statusValue === 'all' || item.release_status === statusValue;
                        const haystack = [
                            item.release_key,
                            item.rov_key,
                            item.ke,
                            item.release_status,
                            item.system_name,
                            ...(Array.isArray(item.release_name_lines) ? item.release_name_lines : [])
                        ].join(' ').toLowerCase();
                        const matchesSearch = !searchValue || haystack.includes(searchValue);

                        return matchesView && matchesStatus && matchesSearch;
                    });

                    if (body) {
                        body.innerHTML = filteredItems.map(buildReleaseRow).join('');
                    }

                    if (visibleCounter) {
                        visibleCounter.textContent = filteredItems.length;
                    }
                    if (tableWrap) {
                        tableWrap.style.display = filteredItems.length ? '' : 'none';
                        tableWrap.classList.toggle('compact', !isReleaseMonitorExpanded);
                    }
                    if (emptyState) {
                        emptyState.style.display = filteredItems.length ? 'none' : '';
                        if (!releaseMonitorItems.length) {
                            updateReleaseEmptyState(releaseMonitorMeta, 'initial');
                        } else if (!yearItems.length) {
                            updateReleaseEmptyState(releaseMonitorMeta, 'empty-year');
                        } else if (!filteredItems.length) {
                            updateReleaseEmptyState(releaseMonitorMeta, 'empty-filter');
                        }
                    }
                }

                // Сворачивание колонок (СУП, Логи, Внедрение)
                function toggleColumn(columnId) {
                    const column = document.getElementById(columnId);
                    column.classList.toggle('collapsed');
                }

                // Сворачивание секции дежурных
                function toggleAssigneesSection() {
                    const section = document.getElementById('section-assignees');
                    section.classList.toggle('collapsed');
                }

                // Сворачивание конкретного дежурного
                function toggleAssignee(header) {
                    const content = header.nextElementSibling;
                    const isActive = header.classList.contains('active');

                    document.querySelectorAll('.assignee-header.active').forEach(h => {
                        h.classList.remove('active');
                        h.nextElementSibling.classList.remove('show');
                    });

                    if (!isActive) {
                        header.classList.add('active');
                        content.classList.add('show');
                    }
                }

                function refreshData() {
                    const btn = document.querySelector('.refresh-btn');
                    btn.classList.add('spinning');

                    // Сначала очищаем кэш согласований
                    fetch(getDashboardUrl('approval_cache_clear'), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    })
                    .then(function() {
                        // Затем обновляем данные
                        return fetch(getDashboardUrl('refresh'), {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' }
                        });
                    })
                    .then(function(response) { return response.json(); })
                    .then(function(data) {
                        if (data.success) {
                            // После обновления данных, проверяем согласования
                            // Небольшая задержка для полной загрузки страницы
                            setTimeout(function() {
                                checkAllApprovals();
                            }, 1500);
                            location.reload();
                        } else {
                            alert('Ошибка обновления: ' + (data.error || 'Неизвестная ошибка'));
                            btn.classList.remove('spinning');
                        }
                    })
                    .catch(function(error) {
                        console.error('Error:', error);
                        alert('Ошибка сети при обновлении');
                        btn.classList.remove('spinning');
                    });
                }

                // Автообновление страницы
                setTimeout(function() { location.reload(); }, 3600000);

                // === Фильтрация задач в колонке Логи ===
                let currentLogiFilter = 'all';

                function filterLogiTasks(filterType) {
                    currentLogiFilter = filterType;

                    // Обновляем активную кнопку (только в колонке Логи)
                    const logiColumn = document.getElementById('col-logi');
                    logiColumn.querySelectorAll('.column-filters .filter-btn').forEach(function(btn) {
                        btn.classList.remove('active');
                        if (btn.dataset.filter === filterType) {
                            btn.classList.add('active');
                        }
                    });

                    // Фильтруем задачи
                    const tasks = document.querySelectorAll('#logi-content .task-card');
                    let visibleCount = 0;

                    tasks.forEach(function(task) {
                        if (filterType === 'all') {
                            task.style.display = '';
                            visibleCount++;
                        } else {
                            const taskTypes = task.dataset.taskTypes || '';
                            if (taskTypes.includes(filterType)) {
                                task.style.display = '';
                                visibleCount++;
                            } else {
                                task.style.display = 'none';
                            }
                        }
                    });

                    // Обновляем счетчик
                    document.getElementById('logi-count').textContent = visibleCount;
                }

                // === Фильтрация задач в колонке Внедрение ===
                let currentVnedrenieFilter = 'all';

                function filterVnedrenieTasks(filterType) {
                    currentVnedrenieFilter = filterType;

                    // Обновляем активную кнопку (только в колонке Внедрение)
                    const vnedrenieColumn = document.getElementById('col-vnedrenie');
                    vnedrenieColumn.querySelectorAll('.column-filters .filter-btn').forEach(function(btn) {
                        btn.classList.remove('active');
                        if (btn.dataset.filter === filterType) {
                            btn.classList.add('active');
                        }
                    });

                    // Фильтруем задачи
                    const tasks = document.querySelectorAll('#vnedrenie-content .task-card');
                    let visibleCount = 0;

                    tasks.forEach(function(task) {
                        if (filterType === 'all') {
                            task.style.display = '';
                            visibleCount++;
                        } else {
                            const vnedrenieType = task.dataset.vnedrenieType || '';
                            if (vnedrenieType === filterType) {
                                task.style.display = '';
                                visibleCount++;
                            } else {
                                task.style.display = 'none';
                            }
                        }
                    });

                    // Обновляем счетчик
                    document.getElementById('vnedrenie-count').textContent = visibleCount;
                }

                // === Глобальный поиск ===
                let searchTimeout;

                function handleSearch() {
                    clearTimeout(searchTimeout);
                    searchTimeout = setTimeout(function() {
                        performSearch();
                    }, 300);
                }

                function performSearch() {
                    const query = document.getElementById('globalSearch').value.toLowerCase().trim();

                    if (!query) {
                        // Показываем все задачи
                        document.querySelectorAll('.task-card').forEach(function(task) {
                            task.style.display = '';
                        });
                        updateCounters();
                        return;
                    }

                    // Фильтруем задачи
                    document.querySelectorAll('.task-card').forEach(function(task) {
                        const key = task.dataset.taskKey.toLowerCase();
                        const summary = task.querySelector('.task-summary').textContent.toLowerCase();
                        const assignee = task.querySelector('.task-meta-compact').textContent.toLowerCase();

                        if (key.includes(query) || summary.includes(query) || assignee.includes(query)) {
                            task.style.display = '';
                        } else {
                            task.style.display = 'none';
                        }
                    });

                    updateCounters();
                }

                function updateCounters() {
                    // Обновляем счетчики для каждой колонки
                    ['sup', 'logi', 'vnedrenie'].forEach(function(type) {
                        const visibleTasks = document.querySelectorAll('#' + type + '-content .task-card:not([style*="display: none"])');
                        const counter = document.getElementById(type + '-count');
                        if (counter) {
                            counter.textContent = visibleTasks.length;
                        }
                    });
                }

                // === Сортировка ===
                function handleSort() {
                    const sortType = document.getElementById('sortSelect').value;

                    ['sup-content', 'logi-content', 'vnedrenie-content'].forEach(function(containerId) {
                        const container = document.getElementById(containerId);
                        if (!container) return;

                        const tasks = Array.from(container.querySelectorAll('.task-card'));
                        if (tasks.length === 0) return;

                        tasks.sort(function(a, b) {
                            switch(sortType) {
                                case 'priority':
                                    const priorityOrder = {'Highest': 0, 'High': 1, 'Critical': 1, 'Medium': 2, 'Low': 3, 'Lowest': 4};
                                    const pa = priorityOrder[a.dataset.priority] || 5;
                                    const pb = priorityOrder[b.dataset.priority] || 5;
                                    return pa - pb;

                                case 'created_desc':
                                    return new Date(b.dataset.created) - new Date(a.dataset.created);

                                case 'created_asc':
                                    return new Date(a.dataset.created) - new Date(b.dataset.created);

                                case 'updated':
                                    return new Date(b.dataset.updated) - new Date(a.dataset.updated);

                                default:
                                    return 0;
                            }
                        });

                        // Перемещаем отсортированные элементы
                        tasks.forEach(function(task) {
                            container.appendChild(task);
                        });
                    });
                }

                // === Проверка согласований (вызывается при загрузке и обновлении) ===
                function checkAllApprovals() {
                    // Собираем все ключи задач
                    const allKeys = [];
                    document.querySelectorAll('.task-card[data-task-key]').forEach(function(task) {
                        allKeys.push(task.dataset.taskKey);
                    });

                    if (allKeys.length === 0) {
                        return;
                    }

                    // Сначала сбрасываем все бейджи согласования
                    document.querySelectorAll('.badge-type.approved').forEach(function(badge) {
                        badge.classList.add('hidden');
                    });
                    document.querySelectorAll('.task-card.approved').forEach(function(card) {
                        card.classList.remove('approved');
                    });

                    // Отправляем запрос на проверку с force_refresh=true
                    fetch(getDashboardUrl('approval_check'), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ issue_keys: allKeys, force_refresh: true })
                    })
                    .then(function(response) { return response.json(); })
                    .then(function(data) {
                        if (data.success) {
                            // Применяем результаты
                            for (const key in data.approvals) {
                                if (data.approvals[key]) {
                                    const badge = document.getElementById('approved-' + key);
                                    const taskCard = document.querySelector('.task-card[data-task-key="' + key + '"');

                                    if (badge) {
                                        badge.classList.remove('hidden');
                                    }
                                    if (taskCard) {
                                        taskCard.classList.add('approved');
                                    }
                                }
                            }
                        }
                    })
                    .catch(function(error) {
                        console.error('Error checking approvals:', error);
                    });
                }

                // Запускаем проверку согласований после загрузки страницы
                document.addEventListener('DOMContentLoaded', function() {
                    // Небольшая задержка, чтобы страница полностью загрузилась
                    setTimeout(checkAllApprovals, 1000);
                });

                function filterLogiTasks(filterType) {
                    currentLogiFilter = filterType;

                    // Обновляем активную кнопку
                    document.querySelectorAll('.column-filters .filter-btn').forEach(btn => {
                        btn.classList.remove('active');
                        if (btn.dataset.filter === filterType) {
                            btn.classList.add('active');
                        }
                    });

                    // Фильтруем задачи
                    const tasks = document.querySelectorAll('#logi-content .task-card');
                    let visibleCount = 0;

                    tasks.forEach(function(task) {
                        if (filterType === 'all') {
                            task.style.display = '';
                            visibleCount++;
                        } else {
                            const taskTypes = task.dataset.taskTypes || '';
                            if (taskTypes.includes(filterType)) {
                                task.style.display = '';
                                visibleCount++;
                            } else {
                                task.style.display = 'none';
                            }
                        }
                    });

                    // Обновляем счетчик
                    document.getElementById('logi-count').textContent = visibleCount;
                }

                // === Глобальный поиск ===

                function handleSearch() {
                    clearTimeout(searchTimeout);
                    searchTimeout = setTimeout(function() {
                        performSearch();
                    }, 300);
                }

                function performSearch() {
                    const query = document.getElementById('globalSearch').value.toLowerCase().trim();

                    if (!query) {
                        // Показываем все задачи
                        document.querySelectorAll('.task-card').forEach(task => {
                            task.style.display = '';
                        });
                        updateCounters();
                        return;
                    }

                    // Фильтруем задачи
                    document.querySelectorAll('.task-card').forEach(task => {
                        const key = task.dataset.taskKey.toLowerCase();
                        const summary = task.querySelector('.task-summary').textContent.toLowerCase();
                        const assignee = task.querySelector('.task-meta-compact').textContent.toLowerCase();

                        if (key.includes(query) || summary.includes(query) || assignee.includes(query)) {
                            task.style.display = '';
                        } else {
                            task.style.display = 'none';
                        }
                    });

                    updateCounters();
                }

                function updateCounters() {
                    // Обновляем счетчики для каждой колонки
                    ['sup', 'logi', 'vnedrenie'].forEach(type => {
                        const visibleTasks = document.querySelectorAll(`#${type}-content .task-card:not([style*="display: none"])`);
                        const counter = document.getElementById(`${type}-count`);
                        if (counter) {
                            counter.textContent = visibleTasks.length;
                        }
                    });
                }

                // === Сортировка ===
                function handleSort() {
                    const sortType = document.getElementById('sortSelect').value;

                    ['sup-content', 'logi-content', 'vnedrenie-content'].forEach(containerId => {
                        const container = document.getElementById(containerId);
                        if (!container) return;

                        const tasks = Array.from(container.querySelectorAll('.task-card'));
                        if (tasks.length === 0) return;

                        tasks.sort(function(a, b) {
                            switch(sortType) {
                                case 'priority':
                                    const priorityOrder = {'Highest': 0, 'High': 1, 'Critical': 1, 'Medium': 2, 'Low': 3, 'Lowest': 4};
                                    const pa = priorityOrder[a.dataset.priority] || 5;
                                    const pb = priorityOrder[b.dataset.priority] || 5;
                                    return pa - pb;

                                case 'created_desc':
                                    return new Date(b.dataset.created) - new Date(a.dataset.created);

                                case 'created_asc':
                                    return new Date(a.dataset.created) - new Date(b.dataset.created);

                                case 'updated':
                                    return new Date(b.dataset.updated) - new Date(a.dataset.updated);

                                default:
                                    return 0;
                            }
                        });

                        // Перемещаем отсортированные элементы
                        tasks.forEach(function(task) {
                            container.appendChild(task);
                        });
                    });
                }

                // === Функции для скрытия/показа задач (Корзина) - Серверная версия ===
                // Данные скрытых задач приходят с сервера
                let hiddenTasksData = dashboardConfig.data.hidden_tasks;

                // Собрать данные о задаче перед скрытием
                function collectTaskData(taskKey) {
                    const taskCard = document.querySelector(`.task-card[data-task-key="${taskKey}"]`);
                    if (!taskCard) return null;

                    const summary = taskCard.querySelector('.task-summary')?.textContent || '';
                    const priority = taskCard.dataset.priority || '';
                    const taskType = taskCard.dataset.taskType || '';

                    return {
                        key: taskKey,
                        summary: summary,
                        priority: priority,
                        type: taskType
                    };
                }

                // Скрыть задачу (отправка на сервер)
                async function hideTask(taskKey) {
                    if (hiddenTasksData[taskKey]) return; // Уже скрыта

                    const taskData = collectTaskData(taskKey);
                    if (!taskData) return;

                    try {
                        const response = await fetch(getDashboardUrl('hidden_tasks'), {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                task_key: taskKey,
                                task_data: taskData
                            })
                        });

                        const result = await response.json();

                        if (result.success) {
                            // Добавляем в локальные данные
                            hiddenTasksData[taskKey] = {
                                key: taskKey,
                                data: taskData,
                                hidden_at: new Date().toISOString()
                            };

                            // Скрыть карточку с анимацией
                            const taskCard = document.querySelector(`.task-card[data-task-key="${taskKey}"]`);
                            if (taskCard) {
                                taskCard.style.transition = 'all 0.3s ease';
                                taskCard.style.opacity = '0';
                                taskCard.style.transform = 'translateX(100px)';
                                setTimeout(() => {
                                    taskCard.style.display = 'none';
                                    updateCounters();
                                    updateTrashButton();
                                }, 300);
                            }
                        }
                    } catch (e) {
                        console.error('Error hiding task:', e);
                    }
                }

                // Показать задачу (восстановление с сервера)
                async function showTask(taskKey) {
                    // Нормализуем ключ задачи (trim)
                    taskKey = taskKey ? taskKey.trim() : '';
                    console.log('showTask called with:', taskKey);

                    if (!taskKey) {
                        console.error('Task key is empty');
                        alert('Ошибка: ключ задачи пустой');
                        return;
                    }

                    // Проверяем наличие задачи в hiddenTasksData
                    if (!hiddenTasksData[taskKey]) {
                        console.warn('Task not found in hiddenTasksData:', taskKey);
                        console.log('Available keys in hiddenTasksData:', Object.keys(hiddenTasksData));
                        alert('Ошибка: задача не найдена в корзине. Возможно, страница устарела. Обновите страницу.');
                        return;
                    }

                    try {
                        // Используем относительный URL без динамического параметра
                        // Добавляем timestamp для сброса кэша
                        const url = getDashboardUrl('hidden_task_restore');
                        console.log('Fetching URL:', url);
                        console.log('TaskKey:', taskKey);
                        console.log('Request body:', JSON.stringify({ task_key: taskKey }));

                        const response = await fetch(url, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ task_key: taskKey })
                        });

                        console.log('Response status:', response.status);

                        // Получаем текст ответа
                        const responseText = await response.text();
                        console.log('Response text (first 500 chars):', responseText.substring(0, 500));

                        // Парсим JSON
                        let result;
                        try {
                            result = JSON.parse(responseText);
                        } catch (parseError) {
                            console.error('Failed to parse JSON. Raw response:', responseText);
                            alert('Ошибка: сервер вернул не JSON. Статус: ' + response.status);
                            return;
                        }
                        console.log('Response result:', result);

                        if (result.success) {
                            // Удаляем из локальных данных
                            delete hiddenTasksData[taskKey];

                            // Показать карточку
                            const taskCard = document.querySelector(`.task-card[data-task-key="${taskKey}"]`);
                            if (taskCard) {
                                taskCard.style.display = '';
                                setTimeout(() => {
                                    taskCard.style.opacity = '1';
                                    taskCard.style.transform = '';
                                }, 10);
                            }

                            // Обновить UI
                            updateTrashPanel();
                            updateTrashButton();
                            updateCounters();
                        } else {
                            console.error('Server returned error:', result.error);
                            alert('Ошибка восстановления: ' + result.error);
                        }
                    } catch (e) {
                        console.error('Error showing task:', e);
                        alert('Ошибка восстановления задачи: ' + e.message);
                    }
                }

                // Восстановить все задачи
                async function restoreAllTasks() {
                    const taskKeys = Object.keys(hiddenTasksData);
                    if (taskKeys.length === 0) return;

                    try {
                        const response = await fetch(getDashboardUrl('hidden_tasks_restore_all'), {
                            method: 'POST'
                        });

                        const result = await response.json();

                        if (result.success) {
                            // Показать все карточки
                            taskKeys.forEach(taskKey => {
                                const taskCard = document.querySelector(`.task-card[data-task-key="${taskKey}"]`);
                                if (taskCard) {
                                    taskCard.style.display = '';
                                    setTimeout(() => {
                                        taskCard.style.opacity = '1';
                                        taskCard.style.transform = '';
                                    }, 10);
                                }
                            });

                            // Очистить локальные данные
                            hiddenTasksData = {};

                            // Обновить UI
                            updateTrashPanel();
                            updateTrashButton();
                            updateCounters();
                        }
                    } catch (e) {
                        console.error('Error restoring all tasks:', e);
                    }
                }

                // Открыть панель корзины
                function openTrashPanel() {
                    trashReturnFocus = document.activeElement;
                    updateTrashPanel();
                    document.getElementById('trashPanelOverlay').classList.add('show');
                    document.getElementById('trashPanel').classList.add('show');
                    document.body.style.overflow = 'hidden';
                }

                // Закрыть панель корзины
                function closeTrashPanel() {
                    document.getElementById('trashPanelOverlay').classList.remove('show');
                    document.getElementById('trashPanel').classList.remove('show');
                    document.body.style.overflow = '';
                    if (trashReturnFocus && typeof trashReturnFocus.focus === 'function') trashReturnFocus.focus();
                    trashReturnFocus = null;
                }

                // Обновить кнопку корзины в навбаре
                function updateTrashButton() {
                    const count = Object.keys(hiddenTasksData).length;
                    const trashBtn = document.getElementById('trashBtn');
                    const trashCount = document.getElementById('trashCount');

                    if (trashCount) {
                        trashCount.textContent = count;
                        trashCount.style.display = count > 0 ? 'flex' : 'none';
                    }

                    if (trashBtn) {
                        trashBtn.classList.toggle('has-items', count > 0);
                        trashBtn.title = `Скрытые задачи (${count})`;
                    }
                }

                // Обновить содержимое панели корзины
                function updateTrashPanel() {
                    const tasks = Object.values(hiddenTasksData);
                    const countEl = document.getElementById('trashPanelCount');
                    const emptyState = document.getElementById('trashEmptyState');
                    const tasksList = document.getElementById('trashTasksList');
                    const restoreAllBtn = document.getElementById('restoreAllBtn');

                    if (countEl) countEl.textContent = tasks.length;

                    if (tasks.length === 0) {
                        if (emptyState) emptyState.style.display = 'block';
                        if (tasksList) tasksList.innerHTML = '';
                        if (restoreAllBtn) restoreAllBtn.disabled = true;
                    } else {
                        if (emptyState) emptyState.style.display = 'none';
                        if (restoreAllBtn) restoreAllBtn.disabled = false;

                        if (tasksList) {
                            tasksList.innerHTML = tasks.map(task => {
                                const data = task.data || {};
                                // Используем data-атрибут для хранения ключа задачи
                                return `
                                    <div class="hidden-task-card" data-task-key="${task.key}">
                                        <div class="hidden-task-key">${task.key}</div>
                                        <div class="hidden-task-summary">${data.summary || ''}</div>
                                        <div class="hidden-task-actions">
                                            <button class="restore-task-btn" data-task-key="${task.key}">
                                                <i class="bi bi-arrow-counterclockwise"></i> Восстановить
                                            </button>
                                        </div>
                                    </div>
                                `;
                            }).join('');

                            // Добавляем обработчики событий после создания HTML
                            tasksList.querySelectorAll('.restore-task-btn').forEach(btn => {
                                btn.addEventListener('click', function(e) {
                                    e.preventDefault();
                                    e.stopPropagation();
                                    const taskKey = this.getAttribute('data-task-key');
                                    console.log('Restore button clicked for task:', taskKey);
                                    if (taskKey) {
                                        showTask(taskKey);
                                    }
                                });
                            });
                        }
                    }
                }

                // Закрыть панель по Escape
                document.addEventListener('keydown', function(e) {
                    if (e.key === 'Escape') closeTrashPanel();
                });

                // Запускаем обновление UI при загрузке страницы
                document.addEventListener('DOMContentLoaded', function() {
                    const releaseMonitorSection = document.getElementById('releaseMonitorSection');
                    const tagsGrid = document.querySelector('.tags-grid');
                    if (releaseMonitorSection && tagsGrid) {
                        tagsGrid.insertAdjacentElement('afterend', releaseMonitorSection);
                    }

                    updateTrashButton();
                    updateCounters();
                    applyReleaseFilters();
                });
        Object.assign(window, {
            applyReleaseFilters,
            closeTrashPanel,
            filterLogiTasks,
            filterVnedrenieTasks,
            handleReleaseSearch,
            handleReleaseViewFilter,
            handleReleaseYearFilter,
            handleSearch,
            handleSort,
            hideTask,
            openTrashPanel,
            refreshData,
            restoreAllTasks,
            showTask,
            toggleAssignee,
            toggleAssigneesSection,
            toggleColumn,
            toggleReleaseMonitorExpand,
            toggleReleaseMonitorSection
        });
        renderReleaseMonitor(
            window.dashboardData.release_monitor || [],
            window.dashboardData.release_monitor_summary || {},
            window.dashboardData.release_monitor_meta || {}
        );
        return true;
    }

    window.initOplotDutyDashboard = initOplotDutyDashboard;
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initOplotDutyDashboard, { once: true });
    } else {
        initOplotDutyDashboard();
    }
}());
