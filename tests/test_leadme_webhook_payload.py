"""Regression coverage for configured LeadMe external-webhook fields."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.crm.levels import level_1_source_reason
from app.webhook.payload import normalize_leadme_payload


_SETTINGS = SimpleNamespace(
    leadme_paid_tags=["facebook", "tiktok"],
    leadme_paid_source_types=["facebook", "tiktok"],
    leadme_incoming_call_tags=["שיחה נכנסת"],
    leadme_incoming_call_source_types=["call"],
    leadme_website_form_tags=["אתר הבית"],
    leadme_website_form_source_types=["form"],
    leadme_website_form_sources=["אתר הבית", "homepage"],
    leadme_website_form_campaigns=["מתעניינים אקדמיה"],
    leadme_landing_page_tags=["עמוד נחיתה", "דף נחיתה"],
    leadme_landing_page_source_types=["lp"],
    leadme_landing_page_campaigns=["דף נחיתה רחפנים"],
    leadme_webinar_tags=["וובינר"],
    leadme_webinar_campaigns=["פניות וובינר"],
    leadme_organic_source_types=["whatsapp"],
    leadme_level_1_sources=["אורגני", "שיחה נכנסת"],
    leadme_level_1_campaigns=["קמפיין ישן"],
)


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

    def test_reads_leadmes_own_hebrew_field_names(self) -> None:
        # Stripping non-ASCII used to collapse every Hebrew key to "", so
        # all of these were silently dropped.
        payload = normalize_leadme_payload(
            {
                "phone": "052-123-4567",
                "מקור הגעה": "lp",
                "סטטוס": ["עמוד נחיתה קורס FPV"],
                "campaignId": "12284",
                "מזהה פייסבוק": "fb-9",
            }
        )

        self.assertEqual(payload.source_type, "lp")
        self.assertEqual(payload.tags, ("עמוד נחיתה קורס FPV",))
        self.assertEqual(payload.campaign, "12284")
        self.assertEqual(payload.facebook_lead_id, "fb-9")

    def test_an_empty_payload_normalizes_instead_of_raising(self) -> None:
        payload = normalize_leadme_payload({})

        self.assertEqual(payload.phone, "")
        self.assertEqual(payload.metadata(), {})


class SourceLevelTests(unittest.TestCase):
    """Which LeadMe tags, sources and campaigns are Level 1 on their own."""

    def setUp(self) -> None:
        patcher = patch("app.crm.levels.get_settings", return_value=_SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_incoming_call_tag(self) -> None:
        self.assertEqual(
            level_1_source_reason({"leadme_tags": ["שיחה נכנסת"]}), "incoming_call"
        )
        self.assertEqual(
            level_1_source_reason({"leadme_source_type": "call"}), "incoming_call"
        )

    def test_website_tag_source_or_campaign(self) -> None:
        self.assertEqual(
            level_1_source_reason({"leadme_tags": ["אתר הבית"]}), "website_form"
        )
        self.assertEqual(
            level_1_source_reason({"leadme_source_type": "form"}), "website_form"
        )
        self.assertEqual(
            level_1_source_reason({"leadme_campaign_id": "מתעניינים אקדמיה"}),
            "website_form",
        )

    def test_landing_page_tags_match_by_containment(self) -> None:
        # The real tags name the page, so an exact match would never fire.
        for tag in (
            "עמוד נחיתה אקדמיה מובייל",
            "עמוד נחיתה קורס FPV",
            "דף נחיתה אוטודרון",
        ):
            with self.subTest(tag=tag):
                self.assertEqual(
                    level_1_source_reason({"leadme_tags": [tag]}), "landing_page"
                )
        self.assertEqual(
            level_1_source_reason({"leadme_source_type": "lp"}), "landing_page"
        )

    def test_webinar_tag_or_campaign(self) -> None:
        self.assertEqual(level_1_source_reason({"leadme_tags": ["וובינר"]}), "webinar")
        self.assertEqual(
            level_1_source_reason({"leadme_campaign": "פניות וובינר"}), "webinar"
        )

    def test_organic_whatsapp_without_paid_tags(self) -> None:
        self.assertEqual(
            level_1_source_reason(
                {"leadme_source_type": "whatsapp", "leadme_tags": ["Whatsapp"]}
            ),
            "organic",
        )

    def test_paid_click_to_whatsapp_is_not_organic(self) -> None:
        # The whole point: a CTWA ad carries the same whatsapp source as a
        # genuine organic arrival, and only the tags separate them.
        for metadata in (
            {
                "leadme_source_type": "whatsapp",
                "leadme_tags": ["Whatsapp", "קמפיין facebook: 6 careers with drones"],
            },
            {"leadme_source_type": "whatsapp", "leadme_tags": ["Facebook"]},
            {"leadme_source_type": "whatsapp", "ctwa_campaign": "עודד"},
        ):
            with self.subTest(metadata=metadata):
                self.assertIsNone(level_1_source_reason(metadata))

    def test_paid_outranks_every_level_one_route(self) -> None:
        self.assertIsNone(
            level_1_source_reason(
                {
                    "leadme_tags": ["אתר הבית", "מודעה facebook: סטורי רוי"],
                    "leadme_campaign": "מתעניינים אקדמיה",
                }
            )
        )

    def test_unknown_sources_are_not_level_one(self) -> None:
        self.assertIsNone(level_1_source_reason({"leadme_tags": ["מוסדות"]}))
        self.assertIsNone(level_1_source_reason({}))


if __name__ == "__main__":
    unittest.main()
