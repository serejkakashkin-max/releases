from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from email.header import Header
from unittest import mock

from tests._support import prepare_config_import

prepare_config_import()

from services.email_to_jira_service import create_email_jira_task
from services.email_to_sbertrack_service import (
    _match_messages,
    _parse_email_message,
    _retry_pending,
)
from services.feature_flags_service import DEFAULT_FEATURE_FLAGS, _normalize_email_to_sbertrack_config
from services.sup_parameters_service import _admin_email_to_sbertrack


class _Response:
    status_code = 201

    @staticmethod
    def json():
        return {"key": "CREATED-1"}


def _existing_emrm_route():
    return {
        "enabled": True,
        "name": "EMRM",
        "target_system": "jira",
        "subject_triggers": ["EMRM"],
        "jira_projects": ["EMRM"],
        "jira_domain": "sberbank",
        "jira_issue_type": "Task",
        "jira_issue_type_id": "3",
        "jira_priority": "Minor",
    }


class StandardEmailJiraRouteTests(unittest.TestCase):
    def test_fresh_default_contains_all_standard_routes(self):
        routes = DEFAULT_FEATURE_FLAGS["automation"]["email_to_sbertrack"]["routes"]
        self.assertEqual(["EMRM", "CLM", "AIST"], [route["name"] for route in routes])

    def test_existing_config_receives_clm_and_aist_once(self):
        config = _normalize_email_to_sbertrack_config({"routes": [_existing_emrm_route()]})
        by_name = {route["name"]: route for route in config["routes"]}

        self.assertEqual({"EMRM", "CLM", "AIST"}, set(by_name))
        self.assertEqual(["SMECLM"], by_name["CLM"]["jira_projects"])
        self.assertEqual("10001", by_name["CLM"]["jira_issue_type_id"])
        self.assertEqual("delta", by_name["AIST"]["jira_domain"])
        self.assertEqual(["SMECSC"], by_name["AIST"]["jira_projects"])
        self.assertEqual("21", by_name["AIST"]["jira_issue_type_id"])
        self.assertEqual("[\u0410\u0438\u0441\u0442] Thunder", by_name["AIST"]["jira_team"]["name"])
        self.assertEqual(1, config["routes_contract_version"])

        admin_config = _admin_email_to_sbertrack({"routes": [_existing_emrm_route()]})
        self.assertEqual(["EMRM", "CLM", "AIST"], [route["name"] for route in admin_config["routes"]])

    def test_saved_contract_respects_admin_route_removal(self):
        config = _normalize_email_to_sbertrack_config(
            {"routes_contract_version": 1, "routes": [_existing_emrm_route()]}
        )
        self.assertEqual(["EMRM"], [route["name"] for route in config["routes"]])


class EmailJiraPayloadTests(unittest.TestCase):
    def _create_and_get_fields(self, route, project):
        event = {
            "space": project,
            "route": route,
            "mail": {"subject": "Test mail", "body": "Body"},
        }
        with (
            mock.patch(
                "services.email_to_jira_service._connection",
                return_value={"url": "https://jira.example", "token": "token"},
            ),
            mock.patch(
                "services.email_to_jira_service.requests.post",
                return_value=_Response(),
            ) as post,
        ):
            create_email_jira_task(event)
        return json.loads(post.call_args.kwargs["data"].decode("utf-8"))["fields"]

    def test_clm_story_minor_has_no_team_field(self):
        fields = self._create_and_get_fields(
            {
                "jira_domain": "sberbank",
                "jira_issue_type": "Story",
                "jira_issue_type_id": "10001",
                "jira_priority": "Minor",
                "jira_team": {},
            },
            "SMECLM",
        )
        self.assertEqual({"key": "SMECLM"}, fields["project"])
        self.assertEqual({"id": "10001"}, fields["issuetype"])
        self.assertEqual({"name": "Minor"}, fields["priority"])
        self.assertNotIn("customfield_12000", fields)

    def test_aist_story_minor_uses_thunder_team(self):
        fields = self._create_and_get_fields(
            {
                "jira_domain": "delta",
                "jira_issue_type": "Story",
                "jira_issue_type_id": "21",
                "jira_priority": "Minor",
                "jira_team": {
                    "field_id": "customfield_12000",
                    "value_id": "12011",
                    "name": "[\u0410\u0438\u0441\u0442] Thunder",
                },
            },
            "SMECSC",
        )
        self.assertEqual({"key": "SMECSC"}, fields["project"])
        self.assertEqual({"id": "21"}, fields["issuetype"])
        self.assertEqual({"name": "Minor"}, fields["priority"])
        self.assertEqual("[\u0410\u0438\u0441\u0442] Thunder", fields["customfield_12000"])


class EmailReplySuppressionTests(unittest.TestCase):
    @staticmethod
    def _raw_message(subject: str, extra_headers: str = "") -> bytes:
        encoded_subject = Header(subject, "utf-8").encode()
        return (
            "From: sender@example.test\r\n"
            "To: automation@example.test\r\n"
            f"Subject: {encoded_subject}\r\n"
            f"Date: {datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')}\r\n"
            "Message-ID: <message@example.test>\r\n"
            f"{extra_headers}"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Body"
        ).encode("utf-8")

    @staticmethod
    def _settings():
        return {
            "technical_mailboxes": ["automation@example.test"],
            "routes": [
                {
                    "enabled": True,
                    "name": "CLM",
                    "target_system": "jira",
                    "jira_domain": "sberbank",
                    "subject_triggers": ["CLM"],
                    "spaces": ["SMECLM"],
                    "suit": "",
                    "priority": "Minor",
                    "summary_template": "{subject}",
                }
            ],
        }

    def test_new_message_still_matches_route(self):
        message = _parse_email_message(1, self._raw_message("CLM: новая задача"), 6000)

        with mock.patch(
            "services.email_to_sbertrack_service.get_sbertrack_users_config",
            return_value={},
        ):
            matches = _match_messages([message], self._settings(), {})

        self.assertFalse(message["is_reply"])
        self.assertEqual(1, len(matches))

    def test_in_reply_to_blocks_route_even_without_re_prefix(self):
        message = _parse_email_message(
            2,
            self._raw_message(
                "CLM: новая задача",
                "In-Reply-To: <original@example.test>\r\n",
            ),
            6000,
        )

        matches = _match_messages([message], self._settings(), {})

        self.assertTrue(message["is_reply"])
        self.assertEqual("in_reply_to", message["reply_reason"])
        self.assertEqual([], matches)

    def test_references_header_blocks_route(self):
        message = _parse_email_message(
            3,
            self._raw_message(
                "AIST: уточнение",
                "References: <original@example.test>\r\n",
            ),
            6000,
        )

        self.assertTrue(message["is_reply"])
        self.assertEqual("references", message["reply_reason"])

    def test_reply_subject_prefix_is_fallback_but_forward_is_not(self):
        reply = _parse_email_message(4, self._raw_message("Re: CLM: новая задача"), 6000)
        localized_reply = _parse_email_message(
            5, self._raw_message("Ответ: AIST: новая задача"), 6000
        )
        forwarded = _parse_email_message(6, self._raw_message("Fwd: CLM: новая задача"), 6000)

        self.assertTrue(reply["is_reply"])
        self.assertTrue(localized_reply["is_reply"])
        self.assertFalse(forwarded["is_reply"])

    def test_legacy_pending_reply_is_removed_without_task_creation(self):
        recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state = {
            "pending": {
                "reply": {
                    "mail": {"subject": "RE: EMRM: исходная тема", "date": recent}
                }
            },
            "created_keys": {},
            "processed_message_ids": [],
        }

        with mock.patch("services.email_to_sbertrack_service._create_task") as create_task:
            created = _retry_pending(
                state,
                {"max_pending_per_cycle": 10},
                {},
            )

        self.assertEqual(0, created)
        self.assertEqual({}, state["pending"])
        create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
