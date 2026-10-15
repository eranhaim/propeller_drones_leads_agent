"""Coverage for recording the delivered video in LeadMe.

Roy, 2026-10-14: the sales team must be able to see which video a lead
already received. Tags are the only per-lead field v3 lets us write, so the
video's name is what lands in the CRM.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.crm.leadme_client import push_video_tag
from app.db.models import Lead


def _settings(**overrides) -> SimpleNamespace:
    base = {"leadme_test_mode": False, "leadme_insert_mode": "update-only"}
    base.update(overrides)
    return SimpleNamespace(**base)


class PushVideoTagTests(unittest.TestCase):
    def _lead(self, phone: str = "972521234567") -> Lead:
        lead = Lead(phone=phone, lead_metadata={}, videos_sent=[])
        lead.id = 1
        return lead

    @patch("app.crm.leadme_client.get_settings")
    def test_a_resolvable_lead_is_tagged_with_the_video_name(
        self,
        get_settings,
    ) -> None:
        get_settings.return_value = _settings()

        with (
            patch("app.crm.leadme_v3.is_v3_available", return_value=True),
            patch("app.crm.leadme_v3.get_lead_id", return_value=13000001),
            patch("app.crm.leadme_v3.add_lead_tag", return_value=True) as add_tag,
        ):
            pushed = push_video_tag(self._lead(), "קריירה בעולם הרחפנים")

        self.assertTrue(pushed)
        add_tag.assert_called_once_with(13000001, "סרטון · קריירה בעולם הרחפנים")

    @patch("app.crm.leadme_client.get_settings")
    def test_an_unresolvable_lead_is_queued_rather_than_lost(
        self,
        get_settings,
    ) -> None:
        """The intro video goes out during the CTWA race, before LeadMe knows
        the phone. That tag has to survive until the queue can write it."""
        get_settings.return_value = _settings()
        lead = self._lead()

        with (
            patch("app.crm.leadme_v3.is_v3_available", return_value=True),
            patch("app.crm.leadme_v3.get_lead_id", return_value=None),
            patch("app.crm.leadme_v3.add_lead_tag") as add_tag,
        ):
            pushed = push_video_tag(lead, "סרטון היכרות", session=Mock())

        self.assertFalse(pushed)
        add_tag.assert_not_called()
        self.assertEqual(
            lead.lead_metadata["leadme_push_pending"],
            [{"kind": "video_tag", "video_title": "סרטון היכרות"}],
        )

    @patch("app.crm.leadme_client.get_settings")
    def test_a_synthetic_harness_phone_never_reaches_leadme(
        self,
        get_settings,
    ) -> None:
        get_settings.return_value = _settings()

        with patch("app.crm.leadme_v3.add_lead_tag") as add_tag:
            self.assertTrue(
                push_video_tag(self._lead("9990000000"), "סרטון היכרות")
            )

        add_tag.assert_not_called()

    @patch("app.crm.leadme_client.get_settings")
    def test_a_video_without_a_name_is_not_tagged(self, get_settings) -> None:
        get_settings.return_value = _settings()

        self.assertFalse(push_video_tag(self._lead(), "  "))


if __name__ == "__main__":
    unittest.main()
