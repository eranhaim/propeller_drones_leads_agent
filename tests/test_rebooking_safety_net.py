"""The booking safety net on a cancel-then-rebook turn.

``cancel_call`` clears ``preferred_call_slot``, so a turn that cancels and
promises a new window but never re-fires ``schedule_call`` reaches
``_enforce_booking_promise`` with no slot captured. It must recover the
window from its own promise -- and it must stay out of the way entirely
when the bot only confirmed a cancellation.
"""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.agent.graph import _booked_slot_in_message, _enforce_booking_promise
from app.db.models import FunnelStage, Lead


def _cancelled_lead() -> Lead:
    """A lead in the state ``cancel_call`` leaves behind."""
    lead = Lead()
    lead.id = 1
    lead.funnel_stage = FunnelStage.warm
    lead.lead_metadata = {}
    return lead


class BookedSlotParsingTests(unittest.TestCase):
    def test_prose_window_is_canonicalised(self) -> None:
        self.assertEqual(
            _booked_slot_in_message("רגע זה טעות, אני רוצה בבוקר בין 9 ל-12"),
            "9-12",
        )

    def test_hyphenated_window_is_read(self) -> None:
        self.assertEqual(_booked_slot_in_message("12-15"), "12-15")

    def test_offering_all_three_windows_names_none(self) -> None:
        self.assertIsNone(
            _booked_slot_in_message("איזה חלון עדיף - 9-12, 12-15, או 15-18?")
        )

    def test_non_canonical_pair_is_not_a_window(self) -> None:
        self.assertIsNone(_booked_slot_in_message("בוא נדבר בין 13 ל-16"))

    def test_plain_cancellation_names_no_window(self) -> None:
        self.assertIsNone(_booked_slot_in_message("ביטלתי את התיאום הקודם."))


class BookingPromiseNetTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in (
            "app.agent.graph.repository",
            "app.agent.graph.mark_engagement",
            "app.agent.graph.mark_call_window",
        ):
            patcher = patch(target)
            setattr(self, target.rsplit(".", 1)[1], patcher.start())
            self.addCleanup(patcher.stop)
        self.mark_engagement.return_value = True
        classify = patch(
            "app.agent.graph.classify_engagement",
            return_value=SimpleNamespace(level=2, reason="test"),
        )
        classify.start()
        self.addCleanup(classify.stop)

    def test_rebooking_promise_recovers_the_window_it_named(self) -> None:
        lead = _cancelled_lead()

        _enforce_booking_promise(
            None,
            lead,
            "סליחה על הבלבול, ביטלתי את התיאום. קבעתי לך שיחה חדשה - "
            "היועץ ייצור איתך קשר בין 9 ל-12.",
        )

        self.mark_call_window.assert_called_once()
        self.assertEqual(self.mark_call_window.call_args.args[1], "9-12")
        self.repository.update_funnel_stage.assert_called_once()
        self.mark_engagement.assert_called_once()

    def test_promise_without_a_window_still_books_with_any(self) -> None:
        lead = _cancelled_lead()

        _enforce_booking_promise(
            None, lead, "קבעתי לך שיחה, יועץ ייצור איתך קשר בקרוב."
        )

        self.assertEqual(self.mark_call_window.call_args.args[1], "any")

    def test_plain_cancellation_is_never_rebooked(self) -> None:
        """The guarantee: no promise in the reply, no booking. Ever."""
        lead = _cancelled_lead()

        _enforce_booking_promise(None, lead, "סליחה על הבלבול, ביטלתי את התיאום הקודם.")

        self.mark_call_window.assert_not_called()
        self.mark_engagement.assert_not_called()
        self.repository.update_funnel_stage.assert_not_called()
        self.assertEqual(lead.funnel_stage, FunnelStage.warm)

    def test_cancellation_naming_the_old_window_is_never_rebooked(self) -> None:
        """A window in the text is not enough -- the reply must promise a call."""
        lead = _cancelled_lead()

        _enforce_booking_promise(
            None, lead, "ביטלתי את השיחה שהייתה בין 15 ל-18, אין בעיה."
        )

        self.mark_call_window.assert_not_called()
        self.repository.update_funnel_stage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
