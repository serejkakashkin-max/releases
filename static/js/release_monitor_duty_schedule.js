(function () {
    const root = document.documentElement;
    const button = document.getElementById('themeToggle');
    const stored = localStorage.getItem('theme');
    const initial = stored === 'light' || stored === 'dark' ? stored : 'dark';

    function applyTheme(theme) {
        root.dataset.theme = theme;
        localStorage.setItem('theme', theme);
        const icon = button && button.querySelector('i');
        if (icon) icon.className = theme === 'dark' ? 'bi bi-sun' : 'bi bi-moon-stars';
    }

    applyTheme(initial);
    if (button) button.addEventListener('click', function () {
        applyTheme(root.dataset.theme === 'dark' ? 'light' : 'dark');
    });

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
}());
