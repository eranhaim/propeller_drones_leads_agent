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
from app.followup import scheduler
from app.webhook.opener import _should_send_website_form_opener
from app.webhook.server import _normalize_phone, app as webhook_app


class PhoneNormalizationTests(unittest.TestCase):
    def test_normalizes_common_israeli_formats(self) -> None:
        self.assertEqual(_normalize_phone("052-123-4567"), "972521234567")
        self.assertEqual(_normalize_phone("+972-052-123-4567"), "972521234567")
        self.assertEqual(_normalize_phone("00972 52 123 4567"), "972521234567")

    def test_rejects_empty_phone(self) -> None:
        self.assertEqual(_normalize_phone("not a phone"), "")


class RefusalTests(unittest.TestCase):
    def test_ambiguous_not_now_is_not_an_opt_out(self) -> None:
        self.assertFalse(_is_refusal("לא כרגע"))

    def test_explicit_stop_is_an_opt_out(self) -> None:
        self.assertTrue(_is_refusal("תסירו אותי בבקשה"))


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

    @patch("app.webhook.server._enqueue_lead")
    @patch("app.webhook.server.get_settings")
    def test_website_campaign_payload_is_queued_for_processing(
        self,
        get_settings,
        enqueue_lead,
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
        queued = enqueue_lead.call_args.args[0]
        self.assertEqual(queued.phone, "972521234567")
        self.assertEqual(queued.name, "Test Lead")
        self.assertEqual(queued.campaign, "מתעניינים אקדמיה")
        self.assertEqual(
            queued.metadata(),
            {
                "leadme_campaign": "מתעניינים אקדמיה",
                "leadme_campaign_id": "מתעניינים אקדמיה",
            },
        )

    @patch("app.webhook.server._enqueue_lead")
    @patch("app.webhook.server.get_settings")
    def test_a_payload_with_only_a_phone_is_still_accepted(
        self,
        get_settings,
        enqueue_lead,
    ) -> None:
        # LeadMe fires at record intake, before the source and campaign
        # exist, so this is the normal delivery rather than a broken one.
        get_settings.return_value = self.settings

        response = self.client.post(
            "/webhook/leadme/webhook-test-secret",
            data={"phone": "052-123-4567"},
        )

        self.assertEqual(response.status_code, 200)
        enqueue_lead.assert_called_once()


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


def _fake_cookie_client(status_code: int, body: str) -> SimpleNamespace:
    """Stand in for the cookie-backed httpx client the canary builds."""
    return SimpleNamespace(
        get=lambda url: None,
        post=lambda url, data: SimpleNamespace(status_code=status_code, text=body),
        cookies=SimpleNamespace(get=lambda name: "csrf-token"),
        close=lambda: None,
    )


class LeakCanaryTests(unittest.TestCase):
    """A canary that cannot read must say so rather than return quietly.

    Both blind paths used to ``return`` without logging, which made a tick
    that read nothing look exactly like a tick that found the trash campaign
    empty. Production ran blind for days on the strength of that.
    """

    def setUp(self) -> None:
        scheduler._LAST_LEAK_COUNT = None
        scheduler._LAST_CANARY_STATUS = None
        self.addCleanup(setattr, scheduler, "_LAST_LEAK_COUNT", None)
        self.addCleanup(setattr, scheduler, "_LAST_CANARY_STATUS", None)

    @patch("app.crm.leadme_delete._build_client", return_value=None)
    def test_a_missing_cookie_session_is_reported(self, _build_client) -> None:
        scheduler._campaign_leak_canary()

        self.assertEqual(scheduler.last_leak_canary_status(), "no_cookies")
        self.assertIsNone(scheduler._LAST_LEAK_COUNT)

    @patch("app.crm.leadme_delete._build_client")
    def test_an_expired_session_is_reported(self, build_client) -> None:
        # Once the cookies die, getPieData redirects to the login page.
        build_client.return_value = _fake_cookie_client(302, "")

        scheduler._campaign_leak_canary()

        self.assertEqual(scheduler.last_leak_canary_status(), "unreadable")
        self.assertIsNone(scheduler._LAST_LEAK_COUNT)

    @patch("app.crm.leadme_delete._build_client")
    def test_a_readable_campaign_records_its_real_count(self, build_client) -> None:
        build_client.return_value = _fake_cookie_client(
            200, '{"result":true,"pieData":{"dataD":[4,3]}}',
        )

        scheduler._campaign_leak_canary()

        self.assertEqual(scheduler.last_leak_canary_status(), "reading")
        self.assertEqual(scheduler._LAST_LEAK_COUNT, 7)

    @patch("app.crm.leadme_delete._build_client")
    def test_an_empty_campaign_is_a_real_zero_not_a_blind_tick(
        self,
        build_client,
    ) -> None:
        build_client.return_value = _fake_cookie_client(
            200, '{"result":true,"pieData":{"dataD":[]}}',
        )

        scheduler._campaign_leak_canary()

        self.assertEqual(scheduler.last_leak_canary_status(), "reading")
        self.assertEqual(scheduler._LAST_LEAK_COUNT, 0)


if __name__ == "__main__":
    unittest.main()
