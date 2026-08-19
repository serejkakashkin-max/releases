from __future__ import annotations

import unittest
from unittest import mock

from tests._support import prepare_config_import

prepare_config_import()

from services.chatbot_service import DashboardChatBot


def _row(
    release_key: str,
    rov_key: str,
    *,
    start: str = "14.08.2026",
    end: str = "15.08.2026",
) -> dict:
    return {
        "release_key": release_key,
        "release_url": f"https://jira.sberbank.ru/browse/{release_key}",
        "rov_key": rov_key,
        "rov_url": f"https://jira.sberbank.ru/browse/{rov_key}",
        "deployment_start": start,
        "deployment_end": end,
    }


class ChatbotReleaseMonitorKeyLookupTests(unittest.TestCase):
    def setUp(self):
        self.bot = DashboardChatBot.__new__(DashboardChatBot)

    def test_release_key_lists_all_linked_rovs_and_deployment_dates(self):
        snapshot = {
            "items": [
                _row("SMECLM-38702", "SMECLM-39001"),
                _row("SMECLM-38702", "SMECLM-39002", start="21.08.2026", end="21.08.2026"),
            ]
        }
        with mock.patch("services.chatbot_service.get_release_monitor_snapshot", return_value=snapshot):
            result = self.bot._handle_release_monitor_key_lookup("  smeclm-38702  ")

        self.assertEqual("release", result["metadata"]["query_type"])
        self.assertIn("https://jira.sberbank.ru/browse/SMECLM-38702", result["text"])
        self.assertIn("https://jira.sberbank.ru/browse/SMECLM-39001", result["text"])
        self.assertIn("14.08.2026 — 15.08.2026", result["text"])
        self.assertIn("https://jira.sberbank.ru/browse/SMECLM-39002", result["text"])
        self.assertIn("дата внедрения: 21.08.2026", result["text"])

    def test_rov_key_lists_all_linked_releases(self):
        snapshot = {
            "items": [
                _row("SMECLM-38702", "SMECLM-39001"),
                _row("SMECLM-38703", "SMECLM-39001", start="16.08.2026", end="17.08.2026"),
            ]
        }
        with mock.patch("services.chatbot_service.get_release_monitor_snapshot", return_value=snapshot):
            result = self.bot._handle_release_monitor_key_lookup("SMECLM-39001")

        self.assertEqual("rov", result["metadata"]["query_type"])
        self.assertIn("https://jira.sberbank.ru/browse/SMECLM-39001", result["text"])
        self.assertIn("https://jira.sberbank.ru/browse/SMECLM-38702", result["text"])
        self.assertIn("https://jira.sberbank.ru/browse/SMECLM-38703", result["text"])
        self.assertIn("16.08.2026 — 17.08.2026", result["text"])

    def test_only_a_standalone_key_activates_local_lookup(self):
        self.assertIsNone(self.bot._handle_release_monitor_key_lookup("покажи SMECLM-38702"))
        self.assertIsNone(self.bot._handle_release_monitor_key_lookup("SMECLM-38702?"))

    def test_unknown_standalone_key_is_answered_locally(self):
        with mock.patch("services.chatbot_service.get_release_monitor_snapshot", return_value={"items": []}):
            result = self.bot._handle_release_monitor_key_lookup("SMECLM-99999")

        self.assertEqual("not_found", result["metadata"]["query_type"])
        self.assertIn("не найден", result["text"])

    def test_process_message_does_not_reach_ai_for_standalone_key(self):
        bot = DashboardChatBot.__new__(DashboardChatBot)
        bot.sessions = {}
        bot.max_context_age_hours = 2
        snapshot = {"items": [_row("SMECLM-38702", "SMECLM-39001")]}
        with (
            mock.patch.object(bot, "_handle_clarification_reply", return_value=None),
            mock.patch.object(bot, "_handle_rov_statistics_command", return_value=None),
            mock.patch.object(bot, "_handle_shift_handover_shortcut", return_value=None),
            mock.patch.object(bot, "_handle_release_agent_command", return_value=None),
            mock.patch("services.chatbot_service.get_release_monitor_snapshot", return_value=snapshot),
            mock.patch.object(
                bot,
                "_resolve_intent_and_message",
                side_effect=AssertionError("AI/intent resolution must not run"),
            ),
        ):
            result = bot.process_message("SMECLM-38702", "session-1")

        self.assertEqual("release_monitor_key_lookup", result["intent"])
        self.assertEqual("release", result["metadata"]["query_type"])

    def test_active_release_flow_keeps_priority_over_lookup(self):
        bot = DashboardChatBot.__new__(DashboardChatBot)
        bot.sessions = {}
        bot.max_context_age_hours = 2
        flow_response = {
            "text": "Продолжаю формирование документов.",
            "intent": "release_document_flow",
            "suggestions": [],
            "metadata": {"state": "next"},
        }
        with (
            mock.patch.object(bot, "_handle_clarification_reply", return_value=None),
            mock.patch.object(bot, "_handle_rov_statistics_command", return_value=None),
            mock.patch.object(bot, "_handle_shift_handover_shortcut", return_value=None),
            mock.patch.object(bot, "_handle_release_agent_command", return_value=flow_response),
            mock.patch.object(
                bot,
                "_handle_release_monitor_key_lookup",
                side_effect=AssertionError("lookup must not intercept an active flow"),
            ),
            mock.patch.object(bot, "_sanitize_suggestions_for_active_flow", return_value=[]),
        ):
            result = bot.process_message("SMECLM-38702", "session-2")

        self.assertEqual("release_document_flow", result["intent"])


if __name__ == "__main__":
    unittest.main()
