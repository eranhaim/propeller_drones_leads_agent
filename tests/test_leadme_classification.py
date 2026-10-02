"""Regression tests for the customer-confirmed LeadMe rule matrix."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.crm.levels import classify_engagement
from app.db.models import FunnelStage, Lead, Message, MessageRole


def _lead(*, metadata=None, stage=FunnelStage.new, messages=None) -> Lead:
    lead = Lead()
    lead.lead_metadata = metadata or {}
    lead.funnel_stage = stage
    lead.messages = messages or []
    return lead


def _user_message(created_at: datetime, content: str = "כן") -> Message:
    message = Message()
    message.role = MessageRole.user
    message.created_at = created_at
    message.content = content
    return message


class LeadMeClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            leadme_organic_sources=["אורגני", "organic"],
            leadme_organic_campaigns=["12293"],
        )

    @patch("app.crm.levels.get_settings")
    def test_organic_source_is_level_one(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(_lead(metadata={"leadme_source": "אורגני"}))
        self.assertEqual((decision.level, decision.reason), (1, "organic_source"))

    @patch("app.crm.levels.get_settings")
    def test_website_form_is_not_level_one(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(
                metadata={"leadme_source": "אתר הבית"},
            )
        )
        self.assertEqual((decision.level, decision.reason), (3, "no_reply"))

    @patch("app.crm.levels.get_settings")
    def test_content_reply_is_level_one(
        self, get_settings,
    ) -> None:
        get_settings.return_value = self.settings
        sent_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        decision = classify_engagement(
            _lead(
                metadata={"video_sent_at": sent_at.isoformat()},
                messages=[_user_message(sent_at + timedelta(minutes=1))],
            )
        )
        self.assertEqual((decision.level, decision.reason), (1, "content_consumed"))

    @patch("app.crm.levels.get_settings")
    def test_shallow_booking_is_level_two(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(
                metadata={"leadme_booking_confirmed": True},
                stage=FunnelStage.handed_off,
                messages=[
                    _user_message(datetime.now(timezone.utc), "כן"),
                    _user_message(datetime.now(timezone.utc), "מחר"),
                    _user_message(datetime.now(timezone.utc), "בבוקר"),
                ],
            )
        )
        self.assertEqual((decision.level, decision.reason), (2, "meaningful_reply"))

    @patch("app.crm.levels.get_settings")
    def test_price_question_is_level_two(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(
                messages=[
                    _user_message(
                        datetime.now(timezone.utc), "כמה עולה הקורס?",
                    )
                ]
            )
        )
        self.assertEqual((decision.level, decision.reason), (2, "meaningful_reply"))

    @patch("app.crm.levels.get_settings")
    def test_ctwa_prefill_without_reply_is_level_three(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(
                metadata={"ctwa_campaign": "עודד"},
                messages=[_user_message(datetime.now(timezone.utc))],
            )
        )
        self.assertEqual((decision.level, decision.reason), (3, "ctwa_no_reply"))

    @patch("app.crm.levels.get_settings")
    def test_reentry_is_level_two_even_without_current_reply(
        self, get_settings,
    ) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(metadata={"leadme_reentry_at": datetime.now(timezone.utc).isoformat()})
        )
        self.assertEqual((decision.level, decision.reason), (2, "reentry_activity"))

    @patch("app.crm.levels.get_settings")
    def test_reentry_ignores_messages_before_session_reset(self, get_settings) -> None:
        get_settings.return_value = self.settings
        now = datetime.now(timezone.utc)
        decision = classify_engagement(
            _lead(
                metadata={
                    "ctwa_campaign": "עודד",
                    "session_reset_at": now.isoformat(),
                },
                messages=[_user_message(now - timedelta(days=8))],
            )
        )
        self.assertEqual((decision.level, decision.reason), (3, "ctwa_no_reply"))

    @patch("app.crm.levels.get_settings")
    def test_explicit_disinterest_is_not_a_numeric_level(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(metadata={"leadme_relevance": "not_relevant"})
        )
        self.assertEqual((decision.level, decision.reason), (None, "explicit_not_interested"))


if __name__ == "__main__":
    unittest.main()
