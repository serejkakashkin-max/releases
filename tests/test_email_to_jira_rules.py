from __future__ import annotations

import json
import unittest
from unittest import mock

from tests._support import prepare_config_import

prepare_config_import()

from services.email_to_jira_service import create_email_jira_task
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


if __name__ == "__main__":
    unittest.main()
