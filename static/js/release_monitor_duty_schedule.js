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
}());
