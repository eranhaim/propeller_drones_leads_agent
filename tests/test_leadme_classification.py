"""Deterministic coverage for the recovered sales classification evidence."""

from datetime import datetime, timedelta, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.crm.leadme_client import push_engagement_level
from app.crm.levels import classify_engagement
from app.db.models import FunnelStage, Lead, Message, MessageRole


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


class LeadMeClassificationTests(unittest.TestCase):
    def test_website_form_starts_at_level_one(self) -> None:
        decision = classify_engagement(
            _lead(metadata={"leadme_source_is_website_form": True})
        )
        self.assertEqual((decision.level, decision.reason), (1, "website_initial"))

    def test_weak_website_followup_downgrades_to_level_two(self) -> None:
        decision = classify_engagement(
            _lead(
                metadata={"leadme_source_is_website_form": True},
                messages=[_user_message(datetime.now(timezone.utc), "כן")],
            )
        )
        self.assertEqual((decision.level, decision.reason), (2, "website_weak_followup"))

    def test_video_without_followup_is_not_level_one(self) -> None:
        sent_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        decision = classify_engagement(
            _lead(metadata={"video_sent_at": sent_at.isoformat()})
        )
        self.assertEqual((decision.level, decision.reason), (3, "no_reply"))

    def test_positive_watched_video_followup_is_level_one(self) -> None:
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
        self.assertEqual((decision.level, decision.reason), (1, "positive_content_followup"))

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

    def test_shallow_booking_is_level_two(self) -> None:
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

    def test_facebook_prefill_without_reply_is_level_three(self) -> None:
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
        self.assertEqual((decision.level, decision.reason), (3, "ctwa_no_reply"))

    def test_explicit_opt_out_is_not_a_numeric_level(self) -> None:
        decision = classify_engagement(
            _lead(
                messages=[
                    _user_message(datetime.now(timezone.utc), "לא תודה, זה לא רלוונטי")
                ]
            )
        )
        self.assertEqual((decision.level, decision.reason), (None, "explicit_not_interested"))

    def test_reentry_is_level_two_not_plain_new(self) -> None:
        decision = classify_engagement(
            _lead(metadata={"leadme_reentry_at": datetime.now(timezone.utc).isoformat()})
        )
        self.assertEqual((decision.level, decision.reason), (2, "reentry_activity"))

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
        self.assertEqual((decision.level, decision.reason), (3, "ctwa_no_reply"))

    def test_legacy_organic_source_is_not_an_l1_shortcut(self) -> None:
        decision = classify_engagement(_lead(metadata={"leadme_source": "אורגני"}))
        self.assertEqual((decision.level, decision.reason), (3, "no_reply"))

    @patch("app.crm.leadme_client.get_settings")
    def test_website_l1_can_downgrade_to_l2_after_weak_followup(
        self,
        get_settings,
    ) -> None:
        get_settings.return_value = SimpleNamespace(leadme_test_mode=True)
        lead = _lead(
            metadata={
                "leadme_source_is_website_form": True,
                "leadme_initial_priority": 1,
                "leadme_last_level": 1,
            }
        )

        self.assertTrue(push_engagement_level(lead, level=2))
        self.assertEqual(lead.lead_metadata["leadme_last_level"], 2)


if __name__ == "__main__":
    unittest.main()