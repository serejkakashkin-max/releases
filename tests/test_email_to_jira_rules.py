from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock

from tests._support import prepare_config_import

prepare_config_import()

from services.email_to_jira_service import create_email_jira_task
from services.email_to_sbertrack_service import (
    _default_state,
    _match_messages,
    _parse_email_message,
    _process_reply_outbox,
    _queue_reply_notification,
    _read_state,
    _reply_message,
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

    def test_reply_notifications_default_on_and_can_be_disabled(self):
        self.assertTrue(
            _normalize_email_to_sbertrack_config({})["reply_notifications_enabled"]
        )
        self.assertFalse(
            _normalize_email_to_sbertrack_config(
                {"reply_notifications_enabled": False}
            )["reply_notifications_enabled"]
        )


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

    def test_only_first_matching_rule_and_destination_are_used(self):
        message = _parse_email_message(1, self._raw_message("CLM AIST: новая задача"), 6000)
        settings = self._settings()
        settings["routes"] = [
            {
                **settings["routes"][0],
                "name": "FIRST",
                "subject_triggers": ["CLM"],
                "spaces": ["FIRST", "SECOND"],
            },
            {
                **settings["routes"][0],
                "name": "LATER",
                "subject_triggers": ["AIST"],
                "spaces": ["LATER"],
            },
        ]

        with mock.patch(
            "services.email_to_sbertrack_service.get_sbertrack_users_config",
            return_value={},
        ):
            matches = _match_messages([message], settings, _default_state())

        self.assertEqual(1, len(matches))
        self.assertEqual("FIRST", matches[0]["route"]["name"])
        self.assertEqual("FIRST", matches[0]["space"])

    def test_first_rule_limit_is_applied_per_message_not_per_cycle(self):
        messages = [
            _parse_email_message(1, self._raw_message("CLM: первая задача"), 6000),
            _parse_email_message(2, self._raw_message("CLM: вторая задача"), 6000),
        ]
        messages[1]["message_id"] = "<second-message@example.test>"
        with mock.patch(
            "services.email_to_sbertrack_service.get_sbertrack_users_config",
            return_value={},
        ):
            matches = _match_messages(messages, self._settings(), _default_state())

        self.assertEqual(2, len(matches))
        self.assertNotEqual(matches[0]["dedupe_key"], matches[1]["dedupe_key"])

    def test_processed_message_stays_deduplicated_after_rule_reordering(self):
        message = _parse_email_message(1, self._raw_message("CLM: новая задача"), 6000)
        state = _default_state()
        state["processed_message_ids"] = [message["message_id"]]
        with mock.patch(
            "services.email_to_sbertrack_service.get_sbertrack_users_config",
            return_value={},
        ):
            self.assertEqual([], _match_messages([message], self._settings(), state))

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


class EmailTaskReplyNotificationTests(unittest.TestCase):
    @staticmethod
    def _event():
        return {
            "dedupe_key": "event-1",
            "message_id": "<source@example.test>",
            "mail": {
                "subject": "CLM: <опасная> тема",
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "thread_message_id": "<source@example.test>",
                "from": [{"name": "Автор", "email": "author@example.test"}],
                "cc": [
                    {"name": "Копия", "email": "copy@example.test"},
                    {"name": "Дубль", "email": "AUTHOR@example.test"},
                    {"name": "Техящик", "email": "automation@example.test"},
                ],
            },
            "route": {"target_system": "jira"},
            "space": "SMECLM",
        }

    @staticmethod
    def _settings(enabled=True):
        return {
            "reply_notifications_enabled": enabled,
            "technical_mailboxes": ["automation@example.test"],
            "max_pending_per_cycle": 10,
        }

    @staticmethod
    def _smtp_settings():
        return {
            "host": "smtp.example.test",
            "port": 587,
            "username": "automation@example.test",
            "password": "secret",
            "sender": "automation@example.test",
            "ssl_verify": True,
        }

    def test_reply_is_threaded_multipart_and_escapes_user_text(self):
        state = _default_state()
        with mock.patch(
            "services.email_to_sbertrack_service._smtp_settings",
            return_value=self._smtp_settings(),
        ):
            _queue_reply_notification(
                state,
                self._event(),
                {
                    "task_key": "SMECLM-40000",
                    "task_url": "https://jira.example.test/browse/SMECLM-40000",
                    "created_at": "2026-08-21T12:00:00+03:00",
                },
                self._settings(),
            )

        notification = state["reply_outbox"]["event-1"]
        self.assertEqual(["author@example.test"], notification["to"])
        self.assertEqual(["copy@example.test"], notification["cc"])
        message = _reply_message(notification, "automation@example.test")
        self.assertEqual("Re: CLM: <опасная> тема", message["Subject"])
        self.assertEqual("<source@example.test>", message["In-Reply-To"])
        self.assertEqual("<source@example.test>", message["References"])
        self.assertEqual("auto-replied", message["Auto-Submitted"])
        self.assertEqual("All", message["X-Auto-Response-Suppress"])
        self.assertTrue(message.is_multipart())
        self.assertIn("SMECLM-40000", message.get_body(preferencelist=("plain",)).get_content())
        html_body = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("CLM: &lt;опасная&gt; тема", html_body)
        self.assertNotIn("CLM: <опасная> тема", html_body)

    def test_smtp_failure_retries_only_reply_not_task_creation(self):
        event = self._event()
        state = _default_state()
        state["pending"] = {event["dedupe_key"]: event}
        result = {
            "task_key": "SMECLM-40001",
            "task_url": "https://jira.example.test/browse/SMECLM-40001",
        }
        checkpoints = []
        with (
            mock.patch(
                "services.email_to_sbertrack_service._create_task", return_value=result
            ) as create_task,
            mock.patch(
                "services.email_to_sbertrack_service._smtp_settings",
                return_value=self._smtp_settings(),
            ),
        ):
            created = _retry_pending(
                state,
                self._settings(),
                {},
                checkpoint=lambda payload: checkpoints.append(dict(payload)),
            )

        self.assertEqual(1, created)
        self.assertEqual({}, state["pending"])
        self.assertIn("event-1", state["created_keys"])
        self.assertIn("event-1", state["reply_outbox"])
        self.assertEqual(1, len(checkpoints))

        with mock.patch(
            "services.email_to_sbertrack_service._send_reply_notification",
            side_effect=RuntimeError("smtp unavailable"),
        ):
            self.assertEqual(0, _process_reply_outbox(state, self._settings()))
        self.assertIn("event-1", state["reply_outbox"])

        self.assertEqual(0, _retry_pending(state, self._settings(), {}))
        create_task.assert_called_once()
        with mock.patch(
            "services.email_to_sbertrack_service._send_reply_notification"
        ) as send_reply:
            self.assertEqual(1, _process_reply_outbox(state, self._settings()))
        send_reply.assert_called_once()
        self.assertEqual({}, state["reply_outbox"])

    def test_disabled_notifications_cancel_existing_outbox(self):
        state = _default_state()
        state["reply_outbox"] = {"event-1": {"task_key": "SMECLM-1"}}
        self.assertEqual(0, _process_reply_outbox(state, self._settings(False)))
        self.assertEqual({}, state["reply_outbox"])

    def test_invalid_task_url_scheme_is_not_queued(self):
        state = _default_state()
        with mock.patch(
            "services.email_to_sbertrack_service._smtp_settings",
            return_value=self._smtp_settings(),
        ):
            _queue_reply_notification(
                state,
                self._event(),
                {"task_key": "SMECLM-2", "task_url": "javascript:alert(1)"},
                self._settings(),
            )
        self.assertEqual({}, state["reply_outbox"])
        self.assertIn("корректный номер или ссылку", state["last_reply_error"])

    def test_legacy_state_is_loaded_as_version_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(
                json.dumps({"version": 1, "created_keys": {"old": {"task_key": "X-1"}}}),
                encoding="utf-8",
            )
            with mock.patch("services.email_to_sbertrack_service.STATE_FILE", path):
                state = _read_state()
        self.assertEqual(2, state["version"])
        self.assertEqual("X-1", state["created_keys"]["old"]["task_key"])
        self.assertEqual({}, state["reply_outbox"])


if __name__ == "__main__":
    unittest.main()
