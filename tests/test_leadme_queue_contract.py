"""Regression tests for LeadMe confirmation and pending-queue semantics."""

import unittest

from app.crm.leadme_queue import record_confirmed_engagement
from app.db.models import Lead


class ConfirmedEngagementTests(unittest.TestCase):
    def test_confirmed_level_removes_satisfied_engagement_only(self) -> None:
        lead = Lead()
        lead.lead_metadata = {
            "leadme_push_pending": [
                {"kind": "engagement", "level": 3},
                {"kind": "ctwa_tag", "campaign": "עודד"},
            ],
            "leadme_push_attempts": 2,
        }

        record_confirmed_engagement(lead, 2)

        self.assertEqual(lead.lead_metadata["leadme_last_level"], 2)
        self.assertEqual(
            lead.lead_metadata["leadme_push_pending"],
            [{"kind": "ctwa_tag", "campaign": "עודד"}],
        )

    def test_confirmed_level_does_not_drop_pending_higher_priority(self) -> None:
        lead = Lead()
        lead.lead_metadata = {
            "leadme_push_pending": [
                {"kind": "engagement", "level": 1, "slot": "9-12"},
                {"kind": "engagement", "level": 3},
            ],
        }

        record_confirmed_engagement(lead, 2)

        self.assertEqual(lead.lead_metadata["leadme_last_level"], 2)
        self.assertEqual(
            lead.lead_metadata["leadme_push_pending"],
            [{"kind": "engagement", "level": 1, "slot": "9-12"}],
        )


if __name__ == "__main__":
    unittest.main()
