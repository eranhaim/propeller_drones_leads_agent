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


def _user_message(created_at: datetime) -> Message:
    message = Message()
    message.role = MessageRole.user
    message.created_at = created_at
    return message


class LeadMeClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            leadme_level_1_sources=["אתר הבית", "שיחה נכנסת", "דף נחיתה"],
            leadme_level_1_campaigns=["מתעניינים אקדמיה"],
        )

    @patch("app.crm.levels.get_settings")
    def test_priority_source_is_level_one(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(_lead(metadata={"leadme_source": "אתר הבית"}))
        self.assertEqual((decision.level, decision.reason), (1, "priority_source"))

    @patch("app.crm.levels.get_settings")
    def test_confirmed_booking_is_level_one(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(
                metadata={"leadme_booking_confirmed": True},
                stage=FunnelStage.handed_off,
            )
        )
        self.assertEqual((decision.level, decision.reason), (1, "booked_call"))

    @patch("app.crm.levels.get_settings")
    def test_price_or_content_reply_stays_level_two_without_booking(
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
        self.assertEqual((decision.level, decision.reason), (2, "content_reply"))

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
    def test_reentry_reply_is_level_two_unless_source_or_booking_is_level_one(
        self, get_settings,
    ) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(messages=[_user_message(datetime.now(timezone.utc))])
        )
        self.assertEqual((decision.level, decision.reason), (2, "meaningful_reply"))

    @patch("app.crm.levels.get_settings")
    def test_explicit_disinterest_is_not_a_numeric_level(self, get_settings) -> None:
        get_settings.return_value = self.settings
        decision = classify_engagement(
            _lead(metadata={"leadme_relevance": "not_relevant"})
        )
        self.assertEqual((decision.level, decision.reason), (None, "explicit_not_interested"))


if __name__ == "__main__":
    unittest.main()
