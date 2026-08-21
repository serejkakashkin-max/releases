from __future__ import annotations

import unittest

from tests._support import PROJECT_ROOT


TEMPLATES = PROJECT_ROOT / "VA" / "schedule_manager" / "templates" / "va_schedule_manager"
STATIC = PROJECT_ROOT / "VA" / "schedule_manager" / "static" / "va_schedule_manager"


class ScheduleManagerOplotShellTests(unittest.TestCase):
    def test_all_pages_inherit_the_shared_shell_and_set_its_header(self):
        pages = (
            "index.html",
            "settings/shifts.html",
            "settings/integrations.html",
            "docs.html",
            "doc_view.html",
        )

        for page in pages:
            with self.subTest(page=page):
                source = (TEMPLATES / page).read_text(encoding="utf-8")
                self.assertIn('{% extends "va_schedule_manager/base.html" %}', source)
                self.assertIn("oplot_page_title", source)
                self.assertIn("oplot_page_description", source)

        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.assertIn('{% extends "layouts/oplot_base.html" %}', base)
        self.assertIn("oplot_show_sidebar = false", base)
        self.assertIn("oplot_topbar_variant = 'core'", base)
        self.assertIn("safe_public_url_for('main.index')", base)
        self.assertIn("{% if topbar_home_url %}", base)
        self.assertIn("К главной Oplot", base)
        self.assertIn("safe_public_url_for('sup_parameters.sup_parameters_page'", base)
        self.assertIn("{% if employees_url %}", base)
        self.assertNotIn("view='competencies'", base)
        self.assertIn('{% block page_actions %}', (TEMPLATES / "index.html").read_text(encoding="utf-8"))

    def test_mutations_keep_csrf_and_modal_overlays_are_clickable(self):
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        index = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        styles = (STATIC / "style.css").read_text(encoding="utf-8")

        self.assertIn('meta name="csrf-token"', base)
        self.assertIn('headers.set("X-CSRF-Token", csrfToken)', base)
        self.assertIn('input[name="_csrf_token"]', base)
        self.assertIn(".va-sm .va-modal-backdrop", styles)
        self.assertIn("position: fixed", styles)
        self.assertIn(".va-sm .va-modal-dialog", styles)
        self.assertNotIn('class="modal-dialog', index)
        self.assertIn("data-file-panel-backdrop", index)
        self.assertIn("data-create-month-backdrop", index)
        self.assertIn("data-copy-month-backdrop", index)
        self.assertIn("event.target.matches(backdropSelector)", script)
        self.assertNotIn("Dark integration overrides", styles)
        self.assertNotIn("var(--focus)", styles)
        self.assertNotIn("var(--border)", styles)

    def test_theme_tokens_do_not_replace_domain_shift_colours(self):
        styles = (STATIC / "style.css").read_text(encoding="utf-8")

        self.assertIn("--panel-soft: var(--oplot-surface-muted)", styles)
        self.assertIn("--warning-hover:", styles)
        self.assertIn("--panel-soft-hover:", styles)
        self.assertIn(".va-sm .shift-8 {\n  background: #eef6ff;\n  color: #175cd3;", styles)
        self.assertIn(".va-sm .shift-дд,\n.va-sm .shift-др {\n  background: #ecfdf3;", styles)
        self.assertIn(".va-sm .autoplan-stop-cell {\n  box-shadow: inset 0 0 0 3px #dc2626;", styles)

    def test_add_employee_modal_excludes_assigned_people_and_closes_on_backdrop(self):
        index = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn('selectattr("employee_name", "equalto", employee.name)', index)
        self.assertIn("data-add-employee-backdrop", index)
        self.assertIn("!employeeSelect.options.length", script)
        self.assertIn("event.target.matches('[data-add-employee-backdrop]')", script)


if __name__ == "__main__":
    unittest.main()
