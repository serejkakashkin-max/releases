(function () {
    'use strict';

    const config = window.RELEASE_ASSIGNMENT_CENTER_CONFIG || {};
    const BASE_PATH = config.basePath || '';
    const POLL_INTERVAL_MS = 15000;
    const NORMAL_TITLE = 'Центр назначений';
    const SESSION_KEY = 'releaseAssignmentCenterNotifications';

    let currentControl = null;
    let lastViewRevision = '';
    let pollInFlight = false;
    let pollTimer = null;
    let blinkTimer = null;
    let blinkPhase = false;
    let toastTimer = null;
    let recommendations = new Map();
    const pendingAssignments = new Map();
    let knownRowKeys = new Set();
    let newRowKeys = new Set();
    let currentWeekKey = '';

    const elements = {};

    function escapeHtml(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#039;');
    }

    function clone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function candidateGroups(control = currentControl) {
        return control?.candidates || { available: [], reserve: [], excluded: [] };
    }

    function allCandidates(control = currentControl) {
        const groups = candidateGroups(control);
        return [
            ...(groups.available || []),
            ...(groups.reserve || []),
            ...(groups.excluded || [])
        ];
    }

    function getCandidate(name, control = currentControl) {
        return allCandidates(control).find(candidate => candidate.name === name) || null;
    }

    function showToast(message, type = '') {
        clearTimeout(toastTimer);
        elements.toast.textContent = message;
        elements.toast.className = `assignment-toast show ${type}`.trim();
        toastTimer = setTimeout(() => {
            elements.toast.className = 'assignment-toast';
        }, 5000);
    }

    function setConnection(state, text) {
        elements.connection.className = `assignment-connection ${state}`;
        const icon = state === 'online'
            ? 'bi-wifi'
            : state === 'offline'
                ? 'bi-wifi-off'
                : 'bi-arrow-repeat';
        elements.connection.innerHTML = `<i class="bi ${icon}"></i> ${escapeHtml(text)}`;
    }

    function loadNotificationState(weekKey, currentKeys) {
        let stored = null;
        try {
            stored = JSON.parse(sessionStorage.getItem(SESSION_KEY) || 'null');
        } catch (_error) {
            stored = null;
        }

        if (!stored || stored.weekKey !== weekKey) {
            knownRowKeys = new Set(currentKeys);
            newRowKeys = new Set();
            currentWeekKey = weekKey;
            saveNotificationState();
            return;
        }

        currentWeekKey = weekKey;
        knownRowKeys = new Set(Array.isArray(stored.knownRowKeys) ? stored.knownRowKeys : []);
        newRowKeys = new Set(Array.isArray(stored.newRowKeys) ? stored.newRowKeys : []);
        const activeKeys = new Set(currentKeys);
        for (const rowKey of [...newRowKeys]) {
            if (!activeKeys.has(rowKey)) {
                newRowKeys.delete(rowKey);
            }
        }
        for (const rowKey of currentKeys) {
            if (!knownRowKeys.has(rowKey)) {
                knownRowKeys.add(rowKey);
                newRowKeys.add(rowKey);
            }
        }
        saveNotificationState();
    }

    function updateNotificationState(weekKey, currentKeys) {
        if (!currentWeekKey) {
            loadNotificationState(weekKey, currentKeys);
            return;
        }
        if (currentWeekKey !== weekKey) {
            loadNotificationState(weekKey, currentKeys);
            return;
        }

        const activeKeys = new Set(currentKeys);
        for (const rowKey of currentKeys) {
            if (!knownRowKeys.has(rowKey)) {
                knownRowKeys.add(rowKey);
                newRowKeys.add(rowKey);
            }
        }
        for (const rowKey of [...newRowKeys]) {
            if (!activeKeys.has(rowKey)) {
                newRowKeys.delete(rowKey);
            }
        }
        saveNotificationState();
    }

    function saveNotificationState() {
        sessionStorage.setItem(SESSION_KEY, JSON.stringify({
            weekKey: currentWeekKey,
            knownRowKeys: [...knownRowKeys],
            newRowKeys: [...newRowKeys]
        }));
        updateTitleAlert();
    }

    function updateTitleAlert() {
        elements.newCount.textContent = String(newRowKeys.size);
        if (!newRowKeys.size) {
            if (blinkTimer) {
                clearInterval(blinkTimer);
                blinkTimer = null;
            }
            document.title = NORMAL_TITLE;
            return;
        }
        if (blinkTimer) {
            return;
        }
        blinkTimer = setInterval(() => {
            blinkPhase = !blinkPhase;
            document.title = blinkPhase
                ? `● Новых без ответственного: ${newRowKeys.size}`
                : NORMAL_TITLE;
        }, 900);
    }

    function buildCandidateOptions(selected = '') {
        const groups = candidateGroups();
        const definitions = [
            ['available', 'Можно назначать'],
            ['reserve', 'Резерв'],
            ['excluded', 'Недоступны по графику']
        ];
        const html = ['<option value="">Выберите ответственного</option>'];
        for (const [groupName, label] of definitions) {
            const candidates = groups[groupName] || [];
            if (!candidates.length) {
                continue;
            }
            html.push(`<optgroup label="${escapeHtml(label)}">`);
            for (const candidate of candidates) {
                const reasons = (candidate.reasons || []).join(', ');
                const suffix = groupName === 'excluded' && reasons ? ` — ${reasons}` : '';
                html.push(
                    `<option value="${escapeHtml(candidate.name)}" ${candidate.name === selected ? 'selected' : ''}>` +
                    `${escapeHtml(candidate.name + suffix)}</option>`
                );
            }
            html.push('</optgroup>');
        }
        return html.join('');
    }

    function itemFingerprint(item) {
        return JSON.stringify([
            item.release_key,
            item.rov_key,
            item.deployment_start,
            item.deployment_end,
            item.release_summary,
            item.system_name,
            item.release_status,
            item.rov_status,
            item.ke_id,
            item.release_version,
            item.duty_owner
        ]);
    }

    function createReleaseCard(item) {
        const card = document.createElement('article');
        card.className = 'assignment-release-card';
        card.dataset.rowKey = item.row_key;
        card.dataset.fingerprint = itemFingerprint(item);
        card.innerHTML = releaseCardHtml(item);
        bindReleaseCard(card);
        return card;
    }

    function releaseCardHtml(item) {
        const recommendation = recommendations.get(item.row_key);
        const releaseLink = item.release_url
            ? `<a href="${escapeHtml(item.release_url)}" target="_blank" rel="noopener">${escapeHtml(item.release_key || '-')}</a>`
            : escapeHtml(item.release_key || '-');
        const rovLink = item.rov_url
            ? `<a href="${escapeHtml(item.rov_url)}" target="_blank" rel="noopener">${escapeHtml(item.rov_key || '-')}</a>`
            : escapeHtml(item.rov_key || '-');
        return `
            <div>
                <div class="assignment-release-title">
                    ${releaseLink}
                    <span>/</span>
                    ${rovLink}
                    <span class="assignment-badge new" data-new-badge ${newRowKeys.has(item.row_key) ? '' : 'hidden'}>Новый</span>
                </div>
                <div class="assignment-release-summary">${escapeHtml(item.release_summary || item.system_name || 'Без названия')}</div>
            </div>
            <div class="assignment-release-facts">
                <div class="assignment-release-fact"><span>Дата</span><strong>${escapeHtml(item.deployment_start || '-')}</strong></div>
                <div class="assignment-release-fact"><span>Система</span><strong>${escapeHtml(item.system_name || '-')}</strong></div>
                <div class="assignment-release-fact"><span>КЭ</span><strong>${escapeHtml(item.ke_id || item.ke_name || '-')}</strong></div>
                <div class="assignment-release-fact"><span>Версия</span><strong>${escapeHtml(item.release_version || '-')}</strong></div>
                <div class="assignment-release-fact"><span>Статус</span><strong>${escapeHtml(item.release_status || item.rov_status || '-')}</strong></div>
                <div class="assignment-release-fact"><span>Дежурный</span><strong>${escapeHtml(item.duty_owner || '-')}</strong></div>
            </div>
            <div class="assignment-row-actions">
                <div class="assignment-row-field">
                    <label>Назначить ответственного</label>
                    <div class="assignment-row-control">
                        <select class="assignment-row-select" aria-label="Ответственный">${buildCandidateOptions()}</select>
                        <button class="assignment-row-apply" type="button" disabled title="Назначить">
                            <i class="bi bi-check2"></i>
                        </button>
                    </div>
                </div>
                <div class="assignment-row-recommendation">
                    ${recommendation
                        ? `GigaChat: <strong>${escapeHtml(recommendation.recommended || '')}</strong>`
                        : 'Можно назначить вручную или запросить рекомендацию.'}
                </div>
            </div>
        `;
    }

    function bindReleaseCard(card) {
        const select = card.querySelector('.assignment-row-select');
        const button = card.querySelector('.assignment-row-apply');
        select.addEventListener('change', () => {
            button.disabled = !select.value;
        });
        button.addEventListener('click', () => {
            if (select.value) {
                assignResponsible(card.dataset.rowKey, select.value);
            }
        });
    }

    function patchReleaseRows(items) {
        const list = elements.releaseList;
        const activeElement = document.activeElement;
        const existing = new Map(
            [...list.querySelectorAll('.assignment-release-card')]
                .map(card => [card.dataset.rowKey, card])
        );
        const activeKeys = new Set(items.map(item => item.row_key));

        for (const [rowKey, card] of existing) {
            if (!activeKeys.has(rowKey)) {
                card.remove();
            }
        }

        for (const item of items) {
            let card = existing.get(item.row_key);
            const nextFingerprint = itemFingerprint(item);
            if (!card) {
                card = createReleaseCard(item);
            } else if (
                card.dataset.fingerprint !== nextFingerprint
                && !card.contains(activeElement)
            ) {
                const replacement = createReleaseCard(item);
                card.replaceWith(replacement);
                card = replacement;
            } else {
                card.classList.toggle('is-new', newRowKeys.has(item.row_key));
                const badge = card.querySelector('[data-new-badge]');
                if (badge) {
                    badge.hidden = !newRowKeys.has(item.row_key);
                }
                const select = card.querySelector('.assignment-row-select');
                if (select && select !== activeElement) {
                    const selected = select.value;
                    select.innerHTML = buildCandidateOptions(selected);
                }
                const rec = recommendations.get(item.row_key);
                const recElement = card.querySelector('.assignment-row-recommendation');
                if (recElement) {
                    recElement.innerHTML = rec
                        ? `GigaChat: <strong>${escapeHtml(rec.recommended || '')}</strong>`
                        : 'Можно назначить вручную или запросить рекомендацию.';
                }
            }
            card.classList.toggle('is-new', newRowKeys.has(item.row_key));
            list.appendChild(card);
        }

        elements.empty.hidden = items.length > 0;
        list.hidden = items.length === 0;
    }

    function candidateMetric(candidate, metricName) {
        return Number(candidate?.metrics?.[metricName] || 0);
    }

    function sortCandidatesByLoad(candidates) {
        return [...(candidates || [])].sort((left, right) =>
            candidateMetric(left, 'week') - candidateMetric(right, 'week')
            || candidateMetric(left, 'active') - candidateMetric(right, 'active')
            || String(left.name || '').localeCompare(String(right.name || ''), 'ru')
        );
    }

    function availableCandidateHtml(candidate, index, maxWeekLoad) {
        const metrics = candidate.metrics || {};
        const weekLoad = Number(metrics.week || 0);
        const loadPercent = maxWeekLoad > 0
            ? Math.max(0, Math.min(100, Math.round((weekLoad / maxWeekLoad) * 100)))
            : 0;
        return `
            <article class="assignment-candidate">
                <div class="assignment-candidate-main">
                    <span class="assignment-candidate-rank">${index + 1}</span>
                    <div class="assignment-candidate-identity">
                        <strong>${escapeHtml(candidate.name)}</strong>
                        <span><i class="bi bi-check-circle-fill"></i> Можно назначать</span>
                    </div>
                    <div class="assignment-week-load" title="Назначений за текущую неделю">
                        <strong>${weekLoad}</strong>
                        <span>за неделю</span>
                    </div>
                </div>
                <div class="assignment-load-track" aria-hidden="true">
                    <span style="width: ${loadPercent}%"></span>
                </div>
                <div class="assignment-load-meta">
                    <span><strong>${Number(metrics.active || 0)}</strong> активно</span>
                    <span><strong>${Number(metrics.quarter || 0)}</strong> квартал</span>
                    <span><strong>${Number(metrics.year || 0)}</strong> год</span>
                </div>
            </article>
        `;
    }

    function availabilityRowHtml(candidate, groupName) {
        const metrics = candidate.metrics || {};
        const reasons = (candidate.reasons || []).join(' · ');
        const isReserve = groupName === 'reserve';
        return `
            <article class="assignment-availability-row ${groupName}">
                <span class="assignment-availability-icon">
                    <i class="bi ${isReserve ? 'bi-shield-exclamation' : 'bi-slash-circle'}"></i>
                </span>
                <div class="assignment-availability-copy">
                    <div class="assignment-availability-name">
                        <strong>${escapeHtml(candidate.name)}</strong>
                        <span class="assignment-person-status ${groupName}">
                            ${isReserve ? 'Резерв' : 'Недоступен'}
                        </span>
                    </div>
                    <p>${escapeHtml(reasons || (isReserve ? 'Резерв по графику' : 'Недоступен по графику'))}</p>
                    <div class="assignment-availability-meta">
                        <span>Активно <strong>${Number(metrics.active || 0)}</strong></span>
                        <span>Квартал <strong>${Number(metrics.quarter || 0)}</strong></span>
                        <span>Год <strong>${Number(metrics.year || 0)}</strong></span>
                    </div>
                </div>
                <div class="assignment-availability-week">
                    <strong>${Number(metrics.week || 0)}</strong>
                    <span>неделя</span>
                </div>
            </article>
        `;
    }

    function renderPeople() {
        const groups = candidateGroups();
        const available = sortCandidatesByLoad(groups.available);
        const reserve = sortCandidatesByLoad(groups.reserve);
        const excluded = [...(groups.excluded || [])].sort((left, right) =>
            String(left.name || '').localeCompare(String(right.name || ''), 'ru')
        );
        const unavailableWasOpen = Boolean(
            elements.peopleList.querySelector('.assignment-unavailable-group[open]')
        );
        const maxWeekLoad = Math.max(
            1,
            ...available.map(candidate => candidateMetric(candidate, 'week'))
        );

        elements.peopleList.innerHTML = `
            <section class="assignment-candidate-section">
                <div class="assignment-person-group-title available">
                    <div>
                        <span>Доступны сейчас</span>
                        <small>От меньшей недельной нагрузки к большей</small>
                    </div>
                    <strong>${available.length}</strong>
                </div>
                <div class="assignment-candidate-list">
                    ${available.length
                        ? available.map((candidate, index) =>
                            availableCandidateHtml(candidate, index, maxWeekLoad)
                        ).join('')
                        : '<div class="assignment-team-empty">Нет доступных сотрудников</div>'}
                </div>
            </section>

            <section class="assignment-reserve-section">
                <div class="assignment-person-group-title reserve">
                    <div>
                        <span>Резерв</span>
                        <small>Можно использовать при необходимости</small>
                    </div>
                    <strong>${reserve.length}</strong>
                </div>
                <div class="assignment-availability-list">
                    ${reserve.length
                        ? reserve.map(candidate => availabilityRowHtml(candidate, 'reserve')).join('')
                        : '<div class="assignment-team-empty">Резерв не назначен</div>'}
                </div>
            </section>

            <details class="assignment-unavailable-group" ${unavailableWasOpen ? 'open' : ''}>
                <summary>
                    <span class="assignment-unavailable-summary-icon">
                        <i class="bi bi-person-x"></i>
                    </span>
                    <span>
                        <strong>Недоступны</strong>
                        <small>Причины ограничений по графику</small>
                    </span>
                    <b>${excluded.length}</b>
                    <i class="bi bi-chevron-down assignment-details-chevron"></i>
                </summary>
                <div class="assignment-availability-list">
                    ${excluded.length
                        ? excluded.map(candidate => availabilityRowHtml(candidate, 'excluded')).join('')
                        : '<div class="assignment-team-empty">Ограничений нет</div>'}
                </div>
            </details>
        `;
    }

    function renderCockpit() {
        const stats = currentControl?.statistics || {};
        const period = currentControl?.period || {};
        const meta = currentControl?.meta || {};
        const missingCount = (currentControl?.missing_responsible || []).length;
        elements.period.textContent = `Период: ${period.label || '-'}`;
        elements.headline.textContent = missingCount
            ? `Нужно распределить: ${missingCount}`
            : 'Назначения на неделю закрыты';
        elements.snapshot.textContent = `Снимок: ${meta.snapshot_at || '-'}`;
        elements.missingCount.textContent = String(missingCount);
        elements.availableCount.textContent = String(stats.available_candidates || 0);
        elements.reserveCount.textContent = String(stats.reserve_candidates || 0);
        elements.excludedCount.textContent = String(stats.excluded_candidates || 0);
        elements.newCount.textContent = String(newRowKeys.size);
    }

    function applyControl(payload, force = false) {
        const meta = payload?.meta || {};
        const allMissing = Array.isArray(payload?.missing_responsible) ? payload.missing_responsible : [];
        const visibleMissing = allMissing.filter(item => !pendingAssignments.has(item.row_key));
        const currentKeys = allMissing.map(item => item.row_key);

        if (!currentWeekKey) {
            loadNotificationState(meta.week_key || '', currentKeys);
        } else {
            updateNotificationState(meta.week_key || '', currentKeys);
        }

        const viewUnchanged = !force && lastViewRevision && lastViewRevision === meta.view_revision;
        currentControl = payload;
        setConnection('online', `Онлайн · ${meta.generated_at || 'обновлено'}`);
        renderCockpit();
        if (viewUnchanged) {
            return;
        }

        patchReleaseRows(visibleMissing);
        renderPeople();
        reconcileRecommendations();
        lastViewRevision = meta.view_revision || '';
    }

    async function fetchControl(force = false) {
        if (!force && pendingAssignments.size) {
            return;
        }
        if (pollInFlight) {
            return;
        }
        pollInFlight = true;
        elements.refreshBtn.disabled = true;
        setConnection('waiting', 'Проверяем данные');
        try {
            const response = await fetch(`${BASE_PATH}/dashboard/release-monitor/assignment-center/data`, {
                cache: 'no-store'
            });
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || 'Не удалось получить данные');
            }
            applyControl(data.control || {}, force);
        } catch (error) {
            console.error('Assignment center polling failed:', error);
            const snapshotAt = currentControl?.meta?.snapshot_at || '-';
            setConnection('offline', `Связь потеряна · данные от ${snapshotAt}`);
        } finally {
            pollInFlight = false;
            elements.refreshBtn.disabled = false;
        }
    }

    function optimisticAssignment(rowKey, responsible) {
        const backup = clone(currentControl);
        const item = (currentControl.missing_responsible || []).find(entry => entry.row_key === rowKey);
        if (!item) {
            return null;
        }
        currentControl.missing_responsible = currentControl.missing_responsible.filter(entry => entry.row_key !== rowKey);
        currentControl.statistics.missing_responsible = Math.max(
            0,
            Number(currentControl.statistics.missing_responsible || 0) - 1
        );
        const candidate = getCandidate(responsible);
        if (candidate) {
            const metrics = candidate.metrics || (candidate.metrics = {});
            metrics.week = Number(metrics.week || 0) + 1;
            metrics.quarter = Number(metrics.quarter || 0) + 1;
            metrics.year = Number(metrics.year || 0) + 1;
            if (!item.is_final) {
                metrics.active = Number(metrics.active || 0) + 1;
            }
        }
        pendingAssignments.set(rowKey, { backup, responsible });
        patchReleaseRows(currentControl.missing_responsible);
        renderPeople();
        renderCockpit();
        return backup;
    }

    async function assignResponsible(rowKey, responsible, options = {}) {
        const candidate = getCandidate(responsible);
        if (!candidate) {
            showToast('Сотрудник отсутствует в актуальном списке кандидатов.', 'error');
            return false;
        }
        if (candidate.availability === 'excluded') {
            const reason = (candidate.reasons || []).join(', ') || 'недоступен по графику';
            if (!window.confirm(`${responsible}: ${reason}. Все равно назначить?`)) {
                return false;
            }
        }

        const backup = optimisticAssignment(rowKey, responsible);
        if (!backup) {
            showToast('Релиз уже исчез из списка без ответственного.', 'error');
            return false;
        }

        try {
            const response = await fetch(`${BASE_PATH}/dashboard/release-monitor/reviewer`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    release_key: rowKey,
                    operation: 'assign_responsible_if_empty',
                    responsible,
                    expected_responsibles: []
                })
            });
            const data = await response.json();
            if (response.status === 409 && data.conflict) {
                pendingAssignments.delete(rowKey);
                newRowKeys.delete(rowKey);
                saveNotificationState();
                const actual = (data.responsibles || []).join(', ') || 'другой сотрудник';
                showToast(`Релиз уже назначен: ${actual}. Ваш выбор не перезаписан.`, 'error');
                lastViewRevision = '';
                if (!options.deferRefresh) {
                    await fetchControl(true);
                }
                return false;
            }
            if (!response.ok || !data.success) {
                throw new Error(data.error || 'Не удалось сохранить назначение');
            }

            pendingAssignments.delete(rowKey);
            newRowKeys.delete(rowKey);
            saveNotificationState();
            showToast(`Ответственный назначен: ${responsible}`, 'success');
            lastViewRevision = '';
            if (!options.deferRefresh) {
                await fetchControl(true);
            }
            return true;
        } catch (error) {
            pendingAssignments.delete(rowKey);
            currentControl = backup;
            patchReleaseRows(currentControl.missing_responsible || []);
            renderPeople();
            renderCockpit();
            showToast(`Назначение не сохранено: ${error.message}`, 'error');
            return false;
        }
    }

    function renderRecommendations(payload) {
        const list = Array.isArray(payload?.recommendations) ? payload.recommendations : [];
        recommendations = new Map(list.map(item => [item.row_key, item]));
        elements.aiSummary.textContent = payload?.summary || payload?.message || '';
        elements.aiList.innerHTML = list.length
            ? list.map(item => `
                <div class="assignment-ai-item" data-row-key="${escapeHtml(item.row_key)}">
                    <div>
                        <strong>${escapeHtml(item.release_key || item.row_key)} → ${escapeHtml(item.recommended || '-')}</strong>
                        <small>
                            ${escapeHtml(item.reason || 'Причина не указана')}
                            · уверенность: ${escapeHtml(item.confidence || 'medium')}
                            ${item.backup ? ` · запасной: ${escapeHtml(item.backup)}` : ''}
                        </small>
                    </div>
                    <button class="assignment-ai-apply" type="button" data-action="apply-recommendation">
                        <i class="bi bi-check2"></i> Применить
                    </button>
                </div>
            `).join('')
            : '<div class="assignment-ai-summary">Нет рекомендаций для применения.</div>';
        elements.applyAllBtn.hidden = list.length === 0;
        elements.aiPanel.hidden = false;
        elements.aiList.querySelectorAll('[data-action="apply-recommendation"]').forEach(button => {
            button.addEventListener('click', async () => {
                const row = button.closest('[data-row-key]');
                const recommendation = recommendations.get(row?.dataset.rowKey || '');
                if (!recommendation) {
                    return;
                }
                button.disabled = true;
                await assignResponsible(recommendation.row_key, recommendation.recommended);
                button.disabled = false;
            });
        });
        patchReleaseRows(currentControl?.missing_responsible || []);
    }

    function reconcileRecommendations() {
        if (!recommendations.size || !currentControl) {
            return;
        }
        const missingKeys = new Set((currentControl.missing_responsible || []).map(item => item.row_key));
        let changed = false;
        for (const rowKey of [...recommendations.keys()]) {
            if (!missingKeys.has(rowKey)) {
                recommendations.delete(rowKey);
                changed = true;
            }
        }
        if (changed) {
            renderRecommendations({
                recommendations: [...recommendations.values()],
                summary: elements.aiSummary.textContent
            });
        }
    }

    async function loadRecommendations() {
        elements.aiBtn.disabled = true;
        elements.aiPanel.hidden = false;
        elements.aiSummary.textContent = 'GigaChat анализирует график, нагрузку и историю назначений...';
        elements.aiList.innerHTML = '';
        try {
            const response = await fetch(`${BASE_PATH}/dashboard/release-monitor/week-control/recommend`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || 'Не удалось получить рекомендации');
            }
            renderRecommendations(data.recommendation || {});
        } catch (error) {
            elements.aiSummary.textContent = `Ошибка GigaChat: ${error.message}`;
            showToast(`GigaChat: ${error.message}`, 'error');
        } finally {
            elements.aiBtn.disabled = false;
        }
    }

    async function applyAllRecommendations() {
        const applicable = [...recommendations.values()].filter(item =>
            (currentControl?.missing_responsible || []).some(row => row.row_key === item.row_key)
        );
        if (!applicable.length) {
            showToast('Нет актуальных рекомендаций для применения.');
            return;
        }
        if (!window.confirm(`Применить ${applicable.length} назначений GigaChat?`)) {
            return;
        }

        elements.applyAllBtn.disabled = true;
        let successCount = 0;
        for (const item of applicable) {
            const success = await assignResponsible(item.row_key, item.recommended, { deferRefresh: true });
            if (success) {
                successCount += 1;
            }
        }
        elements.applyAllBtn.disabled = false;
        await fetchControl(true);
        showToast(`Применено назначений: ${successCount} из ${applicable.length}`, successCount ? 'success' : 'error');
    }

    function cacheElements() {
        elements.period = document.getElementById('assignmentPeriod');
        elements.headline = document.getElementById('assignmentHeadline');
        elements.snapshot = document.getElementById('assignmentSnapshotTime');
        elements.connection = document.getElementById('assignmentConnection');
        elements.missingCount = document.getElementById('assignmentMissingCount');
        elements.newCount = document.getElementById('assignmentNewCount');
        elements.availableCount = document.getElementById('assignmentAvailableCount');
        elements.reserveCount = document.getElementById('assignmentReserveCount');
        elements.excludedCount = document.getElementById('assignmentExcludedCount');
        elements.refreshBtn = document.getElementById('assignmentRefreshBtn');
        elements.aiBtn = document.getElementById('assignmentAiBtn');
        elements.aiPanel = document.getElementById('assignmentAiPanel');
        elements.aiSummary = document.getElementById('assignmentAiSummary');
        elements.aiList = document.getElementById('assignmentAiList');
        elements.applyAllBtn = document.getElementById('assignmentApplyAllBtn');
        elements.releaseList = document.getElementById('assignmentReleaseList');
        elements.empty = document.getElementById('assignmentEmptyState');
        elements.peopleList = document.getElementById('assignmentPeopleList');
        elements.toast = document.getElementById('assignmentToast');
    }

    function startPolling() {
        clearInterval(pollTimer);
        pollTimer = setInterval(() => fetchControl(false), POLL_INTERVAL_MS);
    }

    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) {
            fetchControl(false);
        }
    });

    document.addEventListener('DOMContentLoaded', () => {
        cacheElements();
        elements.refreshBtn.addEventListener('click', () => fetchControl(true));
        elements.aiBtn.addEventListener('click', loadRecommendations);
        elements.applyAllBtn.addEventListener('click', applyAllRecommendations);
        fetchControl(true);
        startPolling();
    });
})();
