from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = ROOT / "templates" / "layouts" / "oplot_base.html"
PERF_CSS = ROOT / "static" / "css" / "oplot_release_performance.css"


class OplotReleasePerformanceTests(unittest.TestCase):
    def test_performance_profile_is_loaded_only_for_release_monitor_endpoint(self):
        source = BASE_TEMPLATE.read_text(encoding="utf-8")
        guard = "request.endpoint == 'dashboard.release_monitor_page'"
        stylesheet = "css/oplot_release_performance.css"

        self.assertIn(guard, source)
        self.assertEqual(source.count(stylesheet), 1)
        self.assertGreater(source.index(stylesheet), source.index("css/oplot_stage9_duty.css"))

    def test_performance_profile_removes_expensive_release_only_effects(self):
        css = PERF_CSS.read_text(encoding="utf-8")

        self.assertIn("body.oplot-body.oplot-release .oplot-topbar--core", css)
        self.assertIn("-webkit-backdrop-filter: none", css)
        self.assertIn("backdrop-filter: none", css)
        self.assertIn("body.oplot-body.oplot-release .release-monitor-table th", css)
        self.assertIn("body.oplot-body.oplot-release .release-monitor-table tbody tr:hover", css)
        self.assertIn("box-shadow: none", css)

    def test_performance_profile_does_not_hide_or_restructure_release_ui(self):
        css = PERF_CSS.read_text(encoding="utf-8")

        forbidden = (
            "display: none",
            "visibility: hidden",
            "content-visibility",
            "position: static",
            "position: absolute",
            "position: fixed",
            "pointer-events: none",
        )
        for declaration in forbidden:
            with self.subTest(declaration=declaration):
                self.assertNotIn(declaration, css)


if __name__ == "__main__":
    unittest.main()
