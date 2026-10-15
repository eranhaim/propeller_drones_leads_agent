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
    _merge_video_tag,
)
from app.agent.graph import (
    _drop_stray_token,
    _ensure_opening_question,
    _is_refusal,
)
from app.db.models import Message, MessageRole
from app.followup import scheduler
from app.webhook.opener import _should_send_website_form_opener
from app.webhook.server import _normalize_phone, app as webhook_app
from app.whatsapp.client import build_greenapi_client


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

    def test_video_tags_deduplicate_by_title_and_keep_other_videos(self) -> None:
        first = {"kind": "video_tag", "video_title": "סרטון היכרות"}
        second = {"kind": "video_tag", "video_title": "קריירה בעולם הרחפנים"}

        self.assertEqual(_merge_video_tag([first], first), [first])
        self.assertEqual(_merge_video_tag([first], second), [first, second])

    def test_not_relevant_status_supersedes_pending_engagement(self) -> None:
        merged = _merge_status(
            [
                {"kind": "engagement", "level": 2},
                {"kind": "ctwa_tag", "campaign": "מאסטר"},
                {"kind": "video_tag", "video_title": "סרטון היכרות"},
            ],
            {"kind": "status", "status_id": "2392"},
        )
        self.assertEqual(
            merged,
            [
                {"kind": "ctwa_tag", "campaign": "מאסטר"},
                {"kind": "video_tag", "video_title": "סרטון היכרות"},
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


class GreenApiRequestBudgetTests(unittest.TestCase):
    """A GreenAPI call must fail in a minute, and must never replay a send.

    The SDK ships ``host_timeout=180`` per attempt and retries every method
    three times, which made one ``sendMessage`` take twelve minutes to fail.
    Two of those in the first-contact opener is what took ``/health`` down for
    18-24 minutes at a time in October 2026.
    """

    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            green_api_instance_id="1101",
            green_api_token="token",
            green_api_host="https://api.green-api.com",
            green_api_media_host="https://media.green-api.com",
            green_api_request_timeout_seconds=60.0,
        )

    @patch("app.whatsapp.client.get_settings")
    def test_a_call_is_capped_at_the_configured_timeout(self, get_settings) -> None:
        get_settings.return_value = self.settings

        api = build_greenapi_client()

        self.assertEqual(api.host_timeout, 60.0)
        self.assertEqual(api.media_timeout, 60.0)

    @patch("app.whatsapp.client.get_settings")
    def test_a_timed_out_send_is_not_replayed(self, get_settings) -> None:
        get_settings.return_value = self.settings

        api = build_greenapi_client()
        retries = api.session.get_adapter("https://api.green-api.com").max_retries

        self.assertEqual(retries.read, 0)
        self.assertEqual(retries.total, 2)
        self.assertEqual(retries.connect, 2)


def _message(role: MessageRole, content: str = "") -> Message:
    message = Message()
    message.role = role
    message.content = content
    return message


class StrayTokenTests(unittest.TestCase):
    """A bare "done" reached a real lead on 2026-10-06."""

    def test_a_reply_that_is_only_a_control_token_is_dropped(self) -> None:
        for token in ("done", "DONE", "[done]", " done. ", "ok", "finished"):
            self.assertEqual(_drop_stray_token(token), "")

    def test_a_real_hebrew_reply_is_untouched(self) -> None:
        reply = "הקורס נפתח בכל חודש. מתי נוח לך שנחזור אליך?"
        self.assertEqual(_drop_stray_token(reply), reply)

    def test_a_reply_that_merely_contains_the_word_is_untouched(self) -> None:
        reply = "סיימנו עם זה, ok? נמשיך לשלב הבא."
        self.assertEqual(_drop_stray_token(reply), reply)


class OpeningQuestionTests(unittest.TestCase):
    """Roy, 2026-10-14: the first reply must leave the lead something to answer."""

    def test_first_reply_without_a_question_gets_the_triage_question(self) -> None:
        reply = _ensure_opening_question(
            "קורס FPV מלמד הטסה בטיסה גופנית ובניית רחפן.",
            [],
        )

        self.assertTrue(reply.endswith("?"))
        self.assertIn("הוצאת רישיון", reply)

    def test_a_first_reply_that_already_asks_is_untouched(self) -> None:
        reply = "קורס FPV הוא קורס עצמאי בן 6 מפגשים. יש לך ניסיון בהטסה?"
        self.assertEqual(_ensure_opening_question(reply, []), reply)

    def test_later_turns_are_left_to_the_prompt(self) -> None:
        reply = "הקורס נמשך שלושה שבועות."
        history = [
            _message(MessageRole.assistant, "היי! אני אלעד מפרופלור דרונס."),
            _message(MessageRole.user, "כמה זמן הקורס?"),
        ]

        self.assertEqual(_ensure_opening_question(reply, history), reply)


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
