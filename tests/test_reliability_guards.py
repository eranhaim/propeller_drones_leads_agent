"""Regression tests for deterministic, dependency-free reliability guards."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.crm.leadme_queue import (
    _is_due,
    _is_expired,
    _merge_ctwa_tag,
    _merge_engagement,
    _merge_status,
)
from app.agent.graph import _is_refusal
from app.webhook.opener import _should_send_website_form_opener
from app.webhook.server import _normalize_phone, app as webhook_app


class PhoneNormalizationTests(unittest.TestCase):
    def test_normalizes_common_israeli_formats(self) -> None:
        self.assertEqual(_normalize_phone("052-123-4567"), "972521234567")
        self.assertEqual(_normalize_phone("+972-052-123-4567"), "972521234567")
        self.assertEqual(_normalize_phone("00972 52 123 4567"), "972521234567")

    def test_rejects_empty_phone(self) -> None:
        self.assertEqual(_normalize_phone("not a phone"), "")


class WebsiteFormOpenerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            website_form_opener_enabled=True,
            leadme_website_form_sources=["אתר הבית", "דף נחיתה"],
            leadme_website_form_campaigns=["מתעניינים אקדמיה"],
        )

    @patch("app.webhook.opener.get_settings")
    def test_new_website_form_lead_gets_one_opener(self, get_settings) -> None:
        get_settings.return_value = self.settings

        self.assertTrue(
            _should_send_website_form_opener(
                "אתר הבית", "", {}, [], is_new_lead=True,
            )
        )

    @patch("app.webhook.opener.get_settings")
    def test_website_campaign_without_source_gets_one_opener(
        self,
        get_settings,
    ) -> None:
        get_settings.return_value = self.settings

        self.assertTrue(
            _should_send_website_form_opener(
                "", "מתעניינים אקדמיה", {}, [], is_new_lead=True,
            )
        )

    @patch("app.webhook.opener.get_settings")
    def test_duplicate_or_existing_lead_never_gets_another_opener(
        self,
        get_settings,
    ) -> None:
        get_settings.return_value = self.settings

        self.assertFalse(
            _should_send_website_form_opener(
                "דף נחיתה", "",
                {"opener_sent_at": "2026-09-28T10:00:00+00:00"},
                [],
                is_new_lead=True,
            )
        )
        self.assertFalse(
            _should_send_website_form_opener(
                "דף נחיתה", "", {}, [object()], is_new_lead=True,
            )
        )
        self.assertFalse(
            _should_send_website_form_opener(
                "דף נחיתה", "", {}, [], is_new_lead=False,
            )
        )

    @patch("app.webhook.opener.get_settings")
    def test_non_website_priority_source_stays_crm_only(self, get_settings) -> None:
        get_settings.return_value = self.settings

        self.assertFalse(
            _should_send_website_form_opener(
                "שיחה נכנסת", "", {}, [], is_new_lead=True,
            )
        )


class LeadMeWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(webhook_app)
        self.settings = SimpleNamespace(webhook_secret="webhook-test-secret")

    @patch("app.webhook.server.handle_new_lead")
    @patch("app.webhook.server.get_settings")
    def test_website_campaign_payload_is_processed_before_accepting(
        self,
        get_settings,
        handle_new_lead,
    ) -> None:
        get_settings.return_value = self.settings

        response = self.client.post(
            "/webhook/leadme/webhook-test-secret",
            data={
                "phone": "052-123-4567",
                "firstname": "Test",
                "lastname": "Lead",
                "campaignId": "מתעניינים אקדמיה",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "accepted"})
        handle_new_lead.assert_called_once_with(
            phone="972521234567",
            name="Test Lead",
            metadata={"leadme_campaign_id": "מתעניינים אקדמיה"},
            campaign_id="מתעניינים אקדמיה",
        )

    @patch("app.webhook.server.handle_new_lead")
    @patch("app.webhook.server.get_settings")
    def test_unsupported_payload_is_not_accepted(
        self,
        get_settings,
        handle_new_lead,
    ) -> None:
        get_settings.return_value = self.settings

        response = self.client.post(
            "/webhook/leadme/webhook-test-secret",
            data={"phone": "052-123-4567"},
        )

        self.assertEqual(response.status_code, 422)
        handle_new_lead.assert_not_called()


class LeadMeQueueTests(unittest.TestCase):
    def test_engagement_merge_preserves_best_level_slot_and_notes(self) -> None:
        existing = [{"kind": "engagement", "level": 2, "note": "replied"}]
        merged = _merge_engagement(
            existing,
            {
                "kind": "engagement",
                "level": 1,
                "slot": "12-15",
                "note": "booked",
            },
        )
        self.assertEqual(
            merged,
            [{
                "kind": "engagement",
                "level": 1,
                "slot": "12-15",
                "note": "booked || replied",
            }],
        )

    def test_ctwa_tags_deduplicate_by_campaign(self) -> None:
        item = {"kind": "ctwa_tag", "campaign": "עודד"}
        self.assertEqual(_merge_ctwa_tag([item], item), [item])

    def test_not_relevant_status_supersedes_pending_engagement(self) -> None:
        merged = _merge_status(
            [
                {"kind": "engagement", "level": 2},
                {"kind": "ctwa_tag", "campaign": "מאסטר"},
            ],
            {"kind": "status", "status_id": "2392"},
        )
        self.assertEqual(
            merged,
            [
                {"kind": "ctwa_tag", "campaign": "מאסטר"},
                {"kind": "status", "status_id": "2392"},
            ],
        )

    def test_due_and_expiry_use_timezone_aware_timestamps(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertTrue(
            _is_due(
                {"leadme_push_next_attempt_at": (now - timedelta(seconds=1)).isoformat()},
                now,
            )
        )
        self.assertFalse(
            _is_due(
                {"leadme_push_next_attempt_at": (now + timedelta(minutes=1)).isoformat()},
                now,
            )
        )
        self.assertTrue(
            _is_expired(
                {
                    "leadme_push_queued_at": (
                        now - timedelta(minutes=181)
                    ).isoformat(),
                    "leadme_push_attempts": 0,
                },
                now,
            )
        )


class RelevanceGuardTests(unittest.TestCase):
    def test_short_explicit_opt_out_is_detected(self) -> None:
        self.assertTrue(_is_refusal("לא רלוונטי, תודה"))
        self.assertTrue(_is_refusal("תסירו אותי"))

    def test_redirect_is_not_treated_as_opt_out(self) -> None:
        self.assertFalse(_is_refusal("לא מעוניין בקורס אלא בשירותי מיפוי לחברה"))


if __name__ == "__main__":
    unittest.main()
