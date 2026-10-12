"""Deterministic coverage for the engagement classification rules.

Mirrors the customer's 2026-10-12 definition: Level 1 is a small group that
already knows the industry, Level 2 is a real conversation including a
booking, Level 3 is the paid-advertising starting point.
"""

from datetime import datetime, timedelta, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.crm.leadme_client import push_engagement_level
from app.crm.levels import classify_engagement
from app.db.models import FunnelStage, Lead, Message, MessageRole


_SETTINGS = SimpleNamespace(
    leadme_website_form_sources=["אתר הבית", "דף נחיתה"],
    leadme_website_form_campaigns=["מתעניינים אקדמיה"],
    leadme_level_1_sources=["אורגני", "שיחה נכנסת"],
    leadme_level_1_campaigns=["פניות וובינר"],
    lead_engagement_fresh_days=14,
    lead_deep_engagement_messages=15,
)


def _lead(*, metadata=None, stage=FunnelStage.new, messages=None) -> Lead:
    lead = Lead()
    lead.lead_metadata = metadata or {}
    lead.funnel_stage = stage
    lead.messages = messages or []
    return lead


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
        self.assertEqual(
            (decision.level, decision.reason), (1, "propeller_content_campaign")
        )

    def test_returning_lead_is_level_one(self) -> None:
        decision = classify_engagement(
            _lead(metadata={"leadme_reentry_at": datetime.now(timezone.utc).isoformat()})
        )
        self.assertEqual((decision.level, decision.reason), (1, "returning_lead"))

    # --- Level 1: demonstrated knowledge ----------------------------------

    def test_long_recent_conversation_with_video_engagement_is_level_one(self) -> None:
        sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
        decision = classify_engagement(
            _lead(
                metadata={"video_sent_at": sent_at.isoformat()},
                messages=_conversation(16, starting_at=sent_at + timedelta(minutes=1)),
            )
        )
        self.assertEqual(
            (decision.level, decision.reason), (1, "deep_recent_engagement")
        )

    def test_long_conversation_without_a_video_is_not_level_one(self) -> None:
        decision = classify_engagement(
            _lead(
                messages=_conversation(
                    20, starting_at=datetime.now(timezone.utc) - timedelta(hours=1)
                )
            )
        )
        self.assertEqual((decision.level, decision.reason), (2, "meaningful_reply"))

    def test_stale_long_conversation_drops_out_of_level_one(self) -> None:
        sent_at = datetime.now(timezone.utc) - timedelta(days=30)
        decision = classify_engagement(
            _lead(
                metadata={"video_sent_at": sent_at.isoformat()},
                messages=_conversation(20, starting_at=sent_at + timedelta(minutes=1)),
            )
        )
        self.assertEqual((decision.level, decision.reason), (2, "meaningful_reply"))

    def test_short_conversation_after_a_video_is_not_level_one(self) -> None:
        sent_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        decision = classify_engagement(
            _lead(
                metadata={"video_sent_at": sent_at.isoformat()},
                messages=[
                    _user_message(
                        sent_at + timedelta(minutes=1),
                        "ראיתי את הסרטון, זה נשמע טוב ואני רוצה להתקדם.",
                    )
                ],
            )
        )
        self.assertEqual((decision.level, decision.reason), (2, "meaningful_reply"))

    # --- Level 2 ----------------------------------------------------------

    def test_booking_a_call_is_level_two_not_level_one(self) -> None:
        decision = classify_engagement(
            _lead(
                stage=FunnelStage.handed_off,
                messages=[
                    _user_message(datetime.now(timezone.utc), "כן"),
                    _user_message(datetime.now(timezone.utc), "רוצה שיחה עם יועץ"),
                    _user_message(datetime.now(timezone.utc), "12-15"),
                ],
            )
        )
        self.assertEqual((decision.level, decision.reason), (2, "meaningful_reply"))

    def test_price_course_and_license_questions_are_level_two(self) -> None:
        for text in (
            "כמה עולה הקורס?",
            "מה לומדים בהכשרה?",
            "איזה רישיון צריך כדי לעבוד?",
        ):
            with self.subTest(text=text):
                decision = classify_engagement(
                    _lead(messages=[_user_message(datetime.now(timezone.utc), text)])
                )
                self.assertEqual(decision.level, 2)

    # --- Level 3 ----------------------------------------------------------

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
