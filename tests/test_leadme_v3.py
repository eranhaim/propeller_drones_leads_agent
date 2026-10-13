"""Regression tests for the official LeadMe v3 status transport."""

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.crm import leadme_v3


def _settings(**values):
    return SimpleNamespace(
        leadme_api_key="test-key",
        leadme_v3_enabled=True,
        leadme_status_level_1="7326",
        leadme_status_level_2="7327",
        leadme_status_level_3="7328",
        **values,
    )


class LeadMeV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        leadme_v3._recent_calls.clear()
        self.addCleanup(leadme_v3._recent_calls.clear)

    @patch("app.crm.leadme_v3.get_settings", return_value=_settings())
    @patch("app.crm.leadme_v3.httpx.Client")
    def test_get_lead_status_uses_documented_post_body(self, client_cls, _settings_mock) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "result": True,
            "leadId": 123,
            "crmLeadId": 456,
            "status": 7326,
            "statusTitle": "חדש - רמה 1",
        }
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = response

        status = leadme_v3.get_lead_status(phone="972521234567")

        self.assertEqual(status["leadId"], 123)
        client.post.assert_called_once_with(
            "https://api.leadmecms.co.il/v3/getLeadStatus",
            headers={
                "LeadMeCMS-API-Key": "test-key",
                "Content-Type": "application/json",
            },
            json={"phone": "0521234567"},
        )

    @patch("app.crm.leadme_v3.get_settings", return_value=_settings())
    @patch("app.crm.leadme_v3.httpx.Client")
    def test_get_lead_tags_uses_live_compatible_get(self, client_cls, _settings_mock) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"result": True, "tags": [{"tag": "מקור: מאסטר"}]}
        client = client_cls.return_value.__enter__.return_value
        client.build_request.return_value = "request"
        client.send.return_value = response

        self.assertEqual(leadme_v3.get_lead_tags(123), ["מקור: מאסטר"])
        client.build_request.assert_called_once_with(
            "GET",
            "https://api.leadmecms.co.il/v3/getLeadTags",
            headers={
                "LeadMeCMS-API-Key": "test-key",
                "Content-Type": "application/json",
            },
            json={"leadId": 123},
        )

    @patch("app.crm.leadme_v3.get_lead_status")
    @patch("app.crm.leadme_v3._post", return_value={"result": True})
    def test_status_write_requires_read_after_write_confirmation(
        self, _post_mock, get_lead_status
    ) -> None:
        get_lead_status.return_value = {
            "leadId": 123,
            "crmLeadId": 456,
            "status": 7326,
            "statusTitle": "חדש - רמה 1",
        }

        self.assertTrue(leadme_v3.update_lead_status(123, 7326))
        _post_mock.assert_called_once_with(
            "updateLeadStatus",
            {"leadId": 123, "status": 7326},
        )
        get_lead_status.assert_called_once_with(lead_id=123)

    @patch("app.crm.leadme_v3.get_lead_status")
    @patch("app.crm.leadme_v3._post", return_value={"result": True})
    def test_status_write_rejects_unconfirmed_result(
        self, _post_mock, get_lead_status
    ) -> None:
        get_lead_status.return_value = {
            "leadId": 123,
            "crmLeadId": 456,
            "status": 7327,
            "statusTitle": "חדש - רמה 2",
        }

        self.assertFalse(leadme_v3.update_lead_status(123, 7326))

    @patch("app.crm.leadme_v3.get_settings", return_value=_settings())
    def test_level_ids_come_from_configuration(self, _settings_mock) -> None:
        self.assertEqual(leadme_v3.status_id_for_level(1), 7326)
        self.assertEqual(leadme_v3.status_id_for_level(3), 7328)
        self.assertEqual(leadme_v3.level_for_status_id(7327), 2)
        self.assertIsNone(leadme_v3.level_for_status_id(1))


class RateLimitTests(unittest.TestCase):
    """LeadMe allows 60 v3 calls a minute and 429s past that.

    A throttled read looks exactly like a lead LeadMe has never heard of, so
    the gate is what keeps a rate limit from being read as missing data.
    """

    def setUp(self) -> None:
        leadme_v3._recent_calls.clear()
        self.addCleanup(leadme_v3._recent_calls.clear)

    def test_the_budget_runs_out_at_the_documented_limit(self) -> None:
        for _ in range(leadme_v3._RATE_LIMIT_PER_MINUTE):
            self.assertTrue(leadme_v3._reserve_rate_slot())

        self.assertEqual(leadme_v3.remaining_rate_budget(), 0)
        with patch("app.crm.leadme_v3.time.sleep"):
            self.assertFalse(leadme_v3._reserve_rate_slot())

    @patch("app.crm.leadme_v3.get_settings", return_value=_settings())
    @patch("app.crm.leadme_v3.httpx.Client")
    def test_an_exhausted_budget_stops_the_call_being_made(
        self, client_cls, _settings_mock
    ) -> None:
        for _ in range(leadme_v3._RATE_LIMIT_PER_MINUTE):
            leadme_v3._reserve_rate_slot()

        with patch("app.crm.leadme_v3.time.sleep"):
            self.assertIsNone(leadme_v3.get_lead_status(phone="972521234567"))
        client_cls.return_value.__enter__.return_value.post.assert_not_called()

    @patch("app.crm.leadme_v3.get_settings", return_value=_settings())
    def test_no_budget_reports_unavailable_rather_than_absent(
        self, _settings_mock
    ) -> None:
        for _ in range(leadme_v3._RATE_LIMIT_PER_MINUTE):
            leadme_v3._reserve_rate_slot()

        self.assertEqual(
            leadme_v3.get_lead_tags_by_phone("972521234567"), ("unavailable", []),
        )

    @patch("app.crm.leadme_v3.get_settings", return_value=_settings())
    @patch("app.crm.leadme_v3.httpx.Client")
    def test_a_429_is_not_read_as_an_answer_about_the_lead(
        self, client_cls, _settings_mock
    ) -> None:
        response = MagicMock(status_code=429)
        response.json.return_value = {
            "result": False, "message": "Rate limit exceeded", "retryAfter": 49,
        }
        client_cls.return_value.__enter__.return_value.post.return_value = response

        self.assertIsNone(leadme_v3.get_lead_status(phone="972521234567"))


if __name__ == "__main__":
    unittest.main()
