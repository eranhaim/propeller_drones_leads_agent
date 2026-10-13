"""Deterministic coverage for the engagement classification rules.

Mirrors the customer's final 2026-10-12 definition: Level 1 is where the lead
came from, or our content plus a call booked for a specific window; Level 2 is
a real conversation, or half of that pair; Level 3 is the paid-advertising
starting point.
"""

from datetime import datetime, timedelta, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.crm.leadme_client import push_engagement_level
from app.crm.levels import classify_engagement
from app.db.models import FunnelStage, Lead, Message, MessageRole


_SETTINGS = SimpleNamespace(
    leadme_paid_tags=["facebook", "tiktok"],
    leadme_paid_source_types=["facebook", "tiktok"],
    leadme_incoming_call_tags=["שיחה נכנסת"],
    leadme_incoming_call_source_types=["call"],
    leadme_website_form_tags=["אתר הבית"],
    leadme_website_form_source_types=["form"],
    leadme_website_form_sources=["אתר הבית"],
    leadme_website_form_campaigns=["מתעניינים אקדמיה"],
    leadme_landing_page_tags=["עמוד נחיתה", "דף נחיתה"],
    leadme_landing_page_source_types=["lp"],
    leadme_landing_page_campaigns=["דף נחיתה רחפנים"],
    leadme_webinar_tags=["וובינר"],
    leadme_webinar_campaigns=["פניות וובינר"],
    leadme_organic_source_types=["whatsapp"],
    leadme_level_1_sources=["אורגני", "שיחה נכנסת"],
    leadme_level_1_campaigns=["קמפיין ישן"],
    lead_engagement_fresh_days=14,
    lead_deep_engagement_messages=12,
    webinar_video_ids=["propeller_webinar"],
)


def _lead(
    *, metadata=None, stage=FunnelStage.new, messages=None, videos_sent=None
) -> Lead:
    lead = Lead()
    lead.lead_metadata = metadata or {}
    lead.funnel_stage = stage
    lead.messages = messages or []
    lead.videos_sent = videos_sent or []
    return lead


def _booked_lead(
    *, slot="12-15", days_ago=0, metadata=None, messages=None, videos_sent=None
) -> Lead:
    """A lead who booked a window but, unless told otherwise, got no content."""
    booked = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return _lead(
        stage=FunnelStage.handed_off,
        metadata={
            "preferred_call_slot": slot,
            "booked_at": booked.isoformat(),
            **(metadata or {}),
        },
        messages=messages or [_user_message(booked, "רוצה שיחה עם יועץ")],
        videos_sent=videos_sent,
    )


def _booked_lead_with_a_video(*, days_ago=0, metadata=None) -> Lead:
    """A lead who engaged with a video and then booked a window."""
    sent_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return _booked_lead(
        days_ago=days_ago,
        metadata={"video_sent_at": sent_at.isoformat(), **(metadata or {})},
        messages=[
            _user_message(
                sent_at + timedelta(minutes=1),
                "ראיתי את הסרטון, מעניין אותי הרישיון המסחרי",
            )
        ],
    )


def _user_message(created_at: datetime, content: str) -> Message:
    message = Message()
    message.role = MessageRole.user
    message.created_at = created_at
    message.content = content
    return message


def _conversation(count: int, *, starting_at: datetime) -> list[Message]:
    return [
        _user_message(
            starting_at + timedelta(minutes=index),
            f"שאלה {index} על הקורס ועל הרישיון",
        )
        for index in range(count)
    ]


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch("app.crm.levels.get_settings", return_value=_SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- Level 1: sources that mean the lead already knows us -------------

    def test_website_form_is_level_one(self) -> None:
        decision = classify_engagement(
            _lead(metadata={"leadme_source_is_website_form": True})
        )
        self.assertEqual((decision.level, decision.reason), (1, "website_form"))

    def test_website_form_stays_level_one_after_a_weak_reply(self) -> None:
        decision = classify_engagement(
            _lead(
                metadata={"leadme_source_is_website_form": True},
                messages=[_user_message(datetime.now(timezone.utc), "כן")],
            )
        )
        self.assertEqual(decision.level, 1)

    def test_organic_source_is_level_one(self) -> None:
        decision = classify_engagement(_lead(metadata={"leadme_source": "אורגני"}))
        self.assertEqual(
            (decision.level, decision.reason), (1, "organic_or_incoming_call")
        )

    def test_incoming_phone_call_is_level_one(self) -> None:
        decision = classify_engagement(
            _lead(metadata={"leadme_source": "שיחה נכנסת"})
        )
        self.assertEqual(
            (decision.level, decision.reason), (1, "organic_or_incoming_call")
        )

    def test_webinar_campaign_is_level_one(self) -> None:
        decision = classify_engagement(
            _lead(metadata={"leadme_campaign": "פניות וובינר"})
        )
        self.assertEqual((decision.level, decision.reason), (1, "webinar"))

    def test_landing_page_tag_is_level_one(self) -> None:
        decision = classify_engagement(
            _lead(metadata={"leadme_tags": ["עמוד נחיתה קורס FPV"]})
        )
        self.assertEqual((decision.level, decision.reason), (1, "landing_page"))

    def test_paid_click_to_whatsapp_is_not_a_level_one_source(self) -> None:
        decision = classify_engagement(
            _lead(
                metadata={
                    "leadme_source_type": "whatsapp",
                    "leadme_tags": ["Whatsapp", "קמפיין facebook: 6 careers"],
                }
            )
        )
        self.assertEqual(
            (decision.level, decision.reason), (3, "paid_ad_no_meaningful_reply")
        )

    def test_organic_whatsapp_is_level_one(self) -> None:
        decision = classify_engagement(
            _lead(
                metadata={
                    "leadme_source_type": "whatsapp",
                    "leadme_tags": ["Whatsapp"],
                }
            )
        )
        self.assertEqual((decision.level, decision.reason), (1, "organic"))

    def test_returning_lead_is_level_one(self) -> None:
        decision = classify_engagement(
            _lead(metadata={"leadme_reentry_at": datetime.now(timezone.utc).isoformat()})
        )
        self.assertEqual((decision.level, decision.reason), (1, "returning_lead"))

    # --- Level 1: our content AND a booked window, while both are fresh ---

    def test_video_engagement_plus_a_booking_is_level_one(self) -> None:
        decision = classify_engagement(_booked_lead_with_a_video())
        self.assertEqual((decision.level, decision.reason), (1, "content_and_booking"))

    def test_the_webinar_plus_a_booking_is_level_one(self) -> None:
        decision = classify_engagement(
            _booked_lead(videos_sent=["propeller_webinar"])
        )
        self.assertEqual((decision.level, decision.reason), (1, "content_and_booking"))

    def test_an_unconfigured_video_is_not_the_webinar(self) -> None:
        decision = classify_engagement(
            _booked_lead(videos_sent=["drone_license_guide"])
        )
        self.assertEqual(
            (decision.level, decision.reason), (2, "booking_without_content")
        )

    def test_a_long_conversation_plus_a_booking_is_level_one(self) -> None:
        now = datetime.now(timezone.utc)
        decision = classify_engagement(
            _booked_lead(messages=_conversation(12, starting_at=now - timedelta(hours=1)))
        )
        self.assertEqual((decision.level, decision.reason), (1, "content_and_booking"))

    def test_a_conversation_below_the_threshold_plus_a_booking_is_level_two(self) -> None:
        now = datetime.now(timezone.utc)
        decision = classify_engagement(
            _booked_lead(messages=_conversation(11, starting_at=now - timedelta(hours=1)))
        )
        self.assertEqual(
            (decision.level, decision.reason), (2, "booking_without_content")
        )

    # --- Level 2: a booked call that misses part of the pair --------------

    def test_a_booking_without_our_content_is_level_two(self) -> None:
        decision = classify_engagement(_booked_lead(days_ago=1))
        self.assertEqual(
            (decision.level, decision.reason), (2, "booking_without_content")
        )

    def test_a_stale_booking_is_level_two_even_with_our_content(self) -> None:
        decision = classify_engagement(_booked_lead_with_a_video(days_ago=30))
        self.assertEqual((decision.level, decision.reason), (2, "stale_booking"))

    def test_a_booking_without_a_specific_window_is_level_two(self) -> None:
        decision = classify_engagement(
            _booked_lead(slot="any", videos_sent=["propeller_webinar"])
        )
        self.assertEqual(
            (decision.level, decision.reason), (2, "booking_without_a_window")
        )

    def test_a_declined_call_is_not_a_booking(self) -> None:
        decision = classify_engagement(
            _booked_lead(slot="none", videos_sent=["propeller_webinar"])
        )
        self.assertEqual(
            (decision.level, decision.reason), (3, "conversation_no_booking")
        )

    # --- Level 3: a conversation that never reached a booking -------------

    def test_our_content_without_a_booking_is_level_three(self) -> None:
        sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
        decision = classify_engagement(
            _lead(
                metadata={"video_sent_at": sent_at.isoformat()},
                messages=_conversation(20, starting_at=sent_at + timedelta(minutes=1)),
            )
        )
        self.assertEqual(
            (decision.level, decision.reason), (3, "conversation_no_booking")
        )

    def test_an_unbooked_lead_is_never_a_booking(self) -> None:
        decision = classify_engagement(
            _lead(
                stage=FunnelStage.warm,
                metadata={"preferred_call_slot": "12-15"},
                messages=[_user_message(datetime.now(timezone.utc), "אולי 12-15")],
            )
        )
        self.assertEqual(
            (decision.level, decision.reason), (3, "conversation_no_booking")
        )

    def test_a_booking_never_outranks_where_the_lead_came_from(self) -> None:
        decision = classify_engagement(
            _booked_lead(days_ago=90, metadata={"leadme_source": "אורגני"})
        )
        self.assertEqual(
            (decision.level, decision.reason), (1, "organic_or_incoming_call")
        )

    def test_last_message_stands_in_for_a_missing_booking_timestamp(self) -> None:
        # Leads booked before booked_at existed.
        old = datetime.now(timezone.utc) - timedelta(days=40)
        lead = _lead(
            stage=FunnelStage.handed_off,
            metadata={"preferred_call_slot": "9-12"},
            messages=[_user_message(old, "רוצה שיחה עם יועץ")],
            videos_sent=["propeller_webinar"],
        )
        self.assertEqual(classify_engagement(lead).reason, "stale_booking")

        lead.messages.append(
            _user_message(datetime.now(timezone.utc), "עדיין מעוניין, מתי נדבר?")
        )
        self.assertEqual(classify_engagement(lead).reason, "content_and_booking")

    # --- Level 3 ----------------------------------------------------------

    def test_price_course_and_license_questions_alone_are_level_three(self) -> None:
        """Asking real questions is a conversation, and a conversation is not
        a booking. These used to be Level 2."""
        for text in (
            "כמה עולה הקורס?",
            "מה לומדים בהכשרה?",
            "איזה רישיון צריך כדי לעבוד?",
        ):
            with self.subTest(text=text):
                decision = classify_engagement(
                    _lead(messages=[_user_message(datetime.now(timezone.utc), text)])
                )
                self.assertEqual(
                    (decision.level, decision.reason),
                    (3, "conversation_no_booking"),
                )


    def test_paid_ad_prefill_without_a_reply_is_level_three(self) -> None:
        decision = classify_engagement(
            _lead(
                metadata={"ctwa_campaign": "עודד"},
                messages=[
                    _user_message(
                        datetime.now(timezone.utc),
                        "אשמח לקבל פרטים על קורס מאסטר רחפנים",
                    )
                ],
            )
        )
        self.assertEqual(
            (decision.level, decision.reason), (3, "paid_ad_no_meaningful_reply")
        )

    def test_lead_with_no_messages_is_level_three(self) -> None:
        self.assertEqual(classify_engagement(_lead()).level, 3)

    def test_pre_reset_messages_do_not_keep_a_lead_hot(self) -> None:
        now = datetime.now(timezone.utc)
        decision = classify_engagement(
            _lead(
                metadata={
                    "ctwa_campaign": "עודד",
                    "session_reset_at": now.isoformat(),
                },
                messages=[_user_message(now - timedelta(days=8), "ראיתי, מעניין")],
            )
        )
        self.assertEqual(
            (decision.level, decision.reason), (3, "paid_ad_no_meaningful_reply")
        )

    # --- Outside the numeric levels ---------------------------------------

    def test_explicit_opt_out_is_not_a_numeric_level(self) -> None:
        decision = classify_engagement(
            _lead(
                messages=[
                    _user_message(datetime.now(timezone.utc), "לא תודה, זה לא רלוונטי")
                ]
            )
        )
        self.assertEqual((decision.level, decision.reason), (None, "explicit_not_interested"))


class LevelTransportTests(unittest.TestCase):
    """Levels move in both directions since 2026-10-12."""

    @patch("app.crm.leadme_client.get_settings")
    def test_level_one_can_be_downgraded_to_level_two(self, get_settings) -> None:
        get_settings.return_value = SimpleNamespace(leadme_test_mode=True)
        lead = Lead()
        lead.lead_metadata = {"leadme_last_level": 1}

        self.assertTrue(push_engagement_level(lead, level=2))
        self.assertEqual(lead.lead_metadata["leadme_last_level"], 2)

    @patch("app.crm.leadme_client.get_settings")
    def test_level_two_can_be_downgraded_to_level_three(self, get_settings) -> None:
        get_settings.return_value = SimpleNamespace(leadme_test_mode=True)
        lead = Lead()
        lead.lead_metadata = {"leadme_last_level": 2}

        self.assertTrue(push_engagement_level(lead, level=3))
        self.assertEqual(lead.lead_metadata["leadme_last_level"], 3)

    @patch("app.crm.leadme_client.get_settings")
    def test_pushing_the_recorded_level_again_is_a_no_op(self, get_settings) -> None:
        get_settings.return_value = SimpleNamespace(leadme_test_mode=True)
        lead = Lead()
        lead.lead_metadata = {"leadme_last_level": 2, "marker": True}

        self.assertTrue(push_engagement_level(lead, level=2))
        self.assertEqual(lead.lead_metadata["leadme_last_level"], 2)


if __name__ == "__main__":
    unittest.main()
