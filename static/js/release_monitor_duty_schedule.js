(function () {
    'use strict';
    let initialized = false;

    function initOplotDutySchedule() {
        if (initialized) return;
        initialized = true;
    const yearSelect = document.getElementById('scheduleYear');
    const monthSelect = document.getElementById('scheduleMonth');
    const periodsNode = document.getElementById('availableScheduleMonths');
    const monthLabels = [
        '', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
        'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'
    ];

    let availablePeriods = [];
    try {
        availablePeriods = JSON.parse(periodsNode?.textContent || '[]');
    } catch (_error) {
        availablePeriods = [];
    }

    function renderAvailableMonths() {
        if (!yearSelect || !monthSelect) return;
        const year = Number(yearSelect.value);
        const previous = Number(monthSelect.value || monthSelect.dataset.selectedMonth);
        const months = availablePeriods
            .filter((period) => Number(period[0]) === year)
            .map((period) => Number(period[1]));

        monthSelect.replaceChildren(...months.map((month) => {
            const option = document.createElement('option');
            option.value = String(month);
            option.textContent = monthLabels[month] || String(month);
            option.selected = month === previous;
            return option;
        }));
        if (!months.includes(previous) && months.length) monthSelect.value = String(months[0]);
    }

    if (yearSelect && monthSelect && availablePeriods.length) {
        yearSelect.addEventListener('change', renderAvailableMonths);
        renderAvailableMonths();
    }

    const autoplanTooltip = document.getElementById('autoplanTooltip');
    let activeHintCell = null;

    function hideAutoplanTooltip() {
        activeHintCell = null;
        if (autoplanTooltip) autoplanTooltip.hidden = true;
    }

    function showAutoplanTooltip(cell) {
        if (!autoplanTooltip || !cell?.dataset.autoplanHint) return;
        activeHintCell = cell;
        autoplanTooltip.textContent = cell.dataset.autoplanHint;
        autoplanTooltip.hidden = false;
        const cellRect = cell.getBoundingClientRect();
        const tooltipRect = autoplanTooltip.getBoundingClientRect();
        const margin = 8;
        let left = cellRect.left + (cellRect.width - tooltipRect.width) / 2;
        left = Math.max(margin, Math.min(left, window.innerWidth - tooltipRect.width - margin));
        let top = cellRect.top - tooltipRect.height - margin;
        if (top < margin) top = cellRect.bottom + margin;
        autoplanTooltip.style.left = `${Math.round(left)}px`;
        autoplanTooltip.style.top = `${Math.round(top)}px`;
    }

    document.addEventListener('pointerover', function (event) {
        const cell = event.target.closest('[data-autoplan-hint]');
        if (cell && cell !== activeHintCell) showAutoplanTooltip(cell);
    });
    document.addEventListener('pointerout', function (event) {
        const cell = event.target.closest('[data-autoplan-hint]');
        if (cell && !cell.contains(event.relatedTarget)) hideAutoplanTooltip();
    });
    document.addEventListener('focusin', function (event) {
        const cell = event.target.closest('[data-autoplan-hint]');
        if (cell) showAutoplanTooltip(cell);
    });
    document.addEventListener('focusout', function (event) {
        if (event.target.closest('[data-autoplan-hint]')) hideAutoplanTooltip();
    });
    window.addEventListener('scroll', hideAutoplanTooltip, { passive: true });
        window.addEventListener('resize', hideAutoplanTooltip);
    }

    window.initOplotDutySchedule = initOplotDutySchedule;
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initOplotDutySchedule, { once: true });
    } else {
        initOplotDutySchedule();
    }
}());
