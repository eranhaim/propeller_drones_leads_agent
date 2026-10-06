"""Regression coverage for configured LeadMe external-webhook fields."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.webhook.opener import _initial_priority
from app.webhook.payload import normalize_leadme_payload


class ConfiguredFieldPayloadTests(unittest.TestCase):
    def test_normalizes_screenshot_fields_and_array_values(self) -> None:
        payload = normalize_leadme_payload(
            {
                "phone": ["052-123-4567"],
                "firstname": ["רוני"],
                "lastname": ["כהן"],
                "campaign": ["מתעניינים אקדמיה"],
                "tags[]": ["טופס אתר", "קורס"],
                "Facebook Lead id": ["facebook-123"],
                "sourceType": ["אתר הבית"],
            }
        )

        self.assertEqual(payload.phone, "972521234567")
        self.assertEqual(payload.name, "רוני כהן")
        self.assertEqual(payload.campaign, "מתעניינים אקדמיה")
        self.assertEqual(payload.tags, ("טופס אתר", "קורס"))
        self.assertEqual(payload.facebook_lead_id, "facebook-123")
        self.assertEqual(payload.source_type, "אתר הבית")
        self.assertEqual(
            payload.metadata(),
            {
                "leadme_campaign": "מתעניינים אקדמיה",
                "leadme_campaign_id": "מתעניינים אקדמיה",
                "leadme_source_type": "אתר הבית",
                "leadme_source": "אתר הבית",
                "leadme_tags": ["טופס אתר", "קורס"],
                "leadme_facebook_lead_id": "facebook-123",
            },
        )

    def test_normalizes_campaign_only_form_payload(self) -> None:
        payload = normalize_leadme_payload(
            {
                "phone": "052-123-4567",
                "firstname": "Test",
                "lastname": "Lead",
                "campaign": "מתעניינים אקדמיה",
            }
        )

        self.assertEqual(payload.phone, "972521234567")
        self.assertEqual(payload.name, "Test Lead")
        self.assertEqual(payload.campaign, "מתעניינים אקדמיה")
        self.assertEqual(payload.source_type, "")


class WebhookPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            leadme_website_form_sources=["אתר הבית", "homepage"],
            leadme_website_form_campaigns=["מתעניינים אקדמיה"],
        )

    @patch("app.webhook.opener.get_settings")
    def test_website_form_source_or_campaign_starts_at_level_one(
        self,
        get_settings,
    ) -> None:
        get_settings.return_value = self.settings

        # Approved website form sources begin L1.
        self.assertEqual(
            _initial_priority("אתר הבית", ""),
            (1, "website_initial"),
        )
        self.assertEqual(
            _initial_priority("", "מתעניינים אקדמיה"),
            (1, "website_initial"),
        )

        # Paid and unknown sources stay L3 until evidence changes them.
        self.assertEqual(
            _initial_priority("Facebook", "אחר"), (3, "unengaged"),
        )
        self.assertEqual(_initial_priority("אורגני", ""), (3, "unengaged"))


if __name__ == "__main__":
    unittest.main()
