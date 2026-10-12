"""Transport coverage for cancelling a booked call in LeadMe."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.crm.leadme_client import last_known_session_status, push_lead_cancellation
from app.db.models import Lead


def _lead() -> Lead:
    lead = Lead()
    lead.phone = "972521234567"
    lead.lead_metadata = {"leadme_last_level": 1}
    return lead


class CancellationTransportTests(unittest.TestCase):
    @patch("app.crm.leadme_client.get_settings")
    @patch("app.crm.leadme_delete._build_client")
    @patch("app.crm.leadme_v3.update_lead_status", return_value=True)
    @patch("app.crm.leadme_v3.get_lead_id", return_value=123)
    @patch("app.crm.leadme_v3.is_v3_available", return_value=True)
    def test_v3_cancels_without_cookies(
        self, _available, _get_lead_id, update_lead_status, build_client, get_settings
    ) -> None:
        get_settings.return_value = SimpleNamespace(leadme_test_mode=False)
        lead = _lead()

        self.assertTrue(push_lead_cancellation(lead, reason="wrong slot"))
        update_lead_status.assert_called_once_with(123, 1)
        build_client.assert_not_called()
        self.assertNotIn("leadme_last_level", lead.lead_metadata)

    @patch("app.crm.leadme_client.get_settings")
    @patch("app.crm.leadme_v3.update_lead_status", return_value=False)
    @patch("app.crm.leadme_v3.get_lead_id", return_value=123)
    @patch("app.crm.leadme_v3.is_v3_available", return_value=True)
    def test_unconfirmed_write_keeps_the_recorded_level(
        self, _available, _get_lead_id, _update, get_settings
    ) -> None:
        get_settings.return_value = SimpleNamespace(leadme_test_mode=False)
        lead = _lead()

        self.assertFalse(push_lead_cancellation(lead))
        self.assertEqual(lead.lead_metadata["leadme_last_level"], 1)

    @patch("app.crm.leadme_client.get_settings")
    @patch("app.crm.leadme_v3.get_lead_id", return_value=None)
    @patch("app.crm.leadme_v3.is_v3_available", return_value=True)
    def test_unknown_phone_is_a_no_op(
        self, _available, _get_lead_id, get_settings
    ) -> None:
        get_settings.return_value = SimpleNamespace(leadme_test_mode=False)
        lead = _lead()

        self.assertTrue(push_lead_cancellation(lead))
        self.assertEqual(lead.lead_metadata["leadme_last_level"], 1)

    @patch("app.crm.leadme_client.get_settings")
    @patch("app.crm.leadme_delete._build_client", return_value=None)
    @patch("app.crm.leadme_v3.is_v3_available", return_value=False)
    def test_without_v3_the_cookie_path_still_reports_failure(
        self, _available, _build_client, get_settings
    ) -> None:
        get_settings.return_value = SimpleNamespace(leadme_test_mode=False)

        self.assertFalse(push_lead_cancellation(_lead()))


class SessionStatusReportingTests(unittest.TestCase):
    def test_last_known_status_reads_the_probe_cache(self) -> None:
        with patch.dict(
            "app.crm.leadme_client._HEALTH_CACHE",
            {"result": {"status": "no_cookies"}},
        ):
            self.assertEqual(last_known_session_status(), "no_cookies")

    def test_last_known_status_is_none_before_the_first_probe(self) -> None:
        with patch.dict("app.crm.leadme_client._HEALTH_CACHE", {"result": None}):
            self.assertIsNone(last_known_session_status())


if __name__ == "__main__":
    unittest.main()
