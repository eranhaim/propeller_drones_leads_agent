"""Regression coverage for configured LeadMe external-webhook fields."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.crm.levels import level_1_source_reason
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


class SourceLevelTests(unittest.TestCase):
    """Which LeadMe source and campaign values are Level 1 on their own."""

    def setUp(self) -> None:
        patcher = patch(
            "app.crm.levels.get_settings",
            return_value=SimpleNamespace(
                leadme_website_form_sources=["אתר הבית", "homepage"],
                leadme_website_form_campaigns=["מתעניינים אקדמיה"],
                leadme_landing_page_campaigns=["דף נחיתה רחפנים"],
                leadme_level_1_sources=["אורגני", "שיחה נכנסת"],
                leadme_level_1_campaigns=["פניות וובינר"],
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_website_form_source_or_campaign(self) -> None:
        self.assertEqual(
            level_1_source_reason({"leadme_source": "אתר הבית"}), "website_form"
        )
        self.assertEqual(
            level_1_source_reason({"leadme_campaign_id": "מתעניינים אקדמיה"}),
            "website_form",
        )

    def test_organic_and_incoming_calls(self) -> None:
        self.assertEqual(
            level_1_source_reason({"leadme_source": "אורגני"}),
            "organic_or_incoming_call",
        )
        self.assertEqual(
            level_1_source_reason({"leadme_source": "שיחה נכנסת"}),
            "organic_or_incoming_call",
        )

    def test_landing_page_campaign(self) -> None:
        self.assertEqual(
            level_1_source_reason({"leadme_campaign": "דף נחיתה רחפנים"}),
            "landing_page",
        )

    def test_webinar_campaign(self) -> None:
        self.assertEqual(
            level_1_source_reason({"leadme_campaign": "פניות וובינר"}),
            "propeller_content_campaign",
        )

    def test_paid_and_unknown_sources_are_not_level_one(self) -> None:
        self.assertIsNone(
            level_1_source_reason({"leadme_source": "Facebook", "leadme_campaign": "אחר"})
        )
        self.assertIsNone(level_1_source_reason({}))


if __name__ == "__main__":
    unittest.main()
