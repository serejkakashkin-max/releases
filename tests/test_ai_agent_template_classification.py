import unittest
from unittest.mock import patch

from services import release_monitor_service
from services.release_report_service import ReleaseReportService


class AiAgentTemplateClassificationTests(unittest.TestCase):
    def test_ai_agents_template_overrides_emrm_prefix(self):
        items = [
            {
                "row_key": "EMRM-100::EMRM-101",
                "release_key": "EMRM-100",
                "release_summary": "EMRM release for a new agent",
                "ke_id": "14290659",
                "source_prefix": "EMRM",
                "system_name": "Фокус",
            }
        ]
        catalog = [
            {
                "category": "AI_AGENTS",
                "release_clean": "AI agent",
                "release_full": "AI agent (14290659)",
                "ke": "14290659",
                "variant": "",
                "aliases": ["AI agent"],
            }
        ]

        with patch.object(release_monitor_service, "build_runtime_template_catalog", return_value=catalog):
            release_monitor_service._apply_template_system_classification(items)

        self.assertTrue(items[0]["is_ai_agent_template"])
        self.assertEqual(items[0]["template_category"], "AI_AGENTS")
        self.assertEqual(items[0]["system_name"], "AI-Агенты")

    def test_ai_agents_template_overrides_aist_prefix_in_week_report(self):
        item = {
            "source_prefix": "SMECSC",
            "system_name": "AI-Агенты",
            "template_category": "AI_AGENTS",
            "is_ai_agent_template": True,
        }

        self.assertEqual(
            ReleaseReportService()._get_item_system_name(item),
            "AI-Агенты",
        )

    def test_legacy_prefix_rule_remains_without_ai_template(self):
        items = [
            {
                "release_key": "SMECSC-100",
                "release_summary": "Regular AIST release",
                "ke_id": "99999999",
                "source_prefix": "SMECSC",
                "system_name": "CLM",
            }
        ]

        with patch.object(release_monitor_service, "build_runtime_template_catalog", return_value=[]):
            release_monitor_service._apply_template_system_classification(items)

        self.assertFalse(items[0]["is_ai_agent_template"])
        self.assertEqual(items[0]["system_name"], "CLM")
        self.assertEqual(
            ReleaseReportService()._normalize_system_name(
                items[0]["system_name"],
                items[0]["source_prefix"],
            ),
            "АИСТ",
        )


if __name__ == "__main__":
    unittest.main()
