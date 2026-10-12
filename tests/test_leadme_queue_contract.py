"""Regression tests for LeadMe confirmation and pending-queue semantics."""

import unittest

from app.crm.leadme_queue import record_confirmed_engagement
from app.db.models import Lead


class ConfirmedEngagementTests(unittest.TestCase):
    def test_confirmed_level_leaves_other_kinds_queued(self) -> None:
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

    def test_confirmed_level_discards_every_queued_engagement(self) -> None:
        """A confirmed write is newer than anything still in the queue."""
        lead = Lead()
        lead.lead_metadata = {
            "leadme_push_pending": [
                {"kind": "engagement", "level": 1, "slot": "9-12"},
                {"kind": "engagement", "level": 3},
            ],
        }

        record_confirmed_engagement(lead, 2)

        self.assertEqual(lead.lead_metadata["leadme_last_level"], 2)
        self.assertNotIn("leadme_push_pending", lead.lead_metadata)

    def test_a_confirmed_downgrade_overwrites_the_recorded_level(self) -> None:
        lead = Lead()
        lead.lead_metadata = {"leadme_last_level": 1}

        record_confirmed_engagement(lead, 3)

        self.assertEqual(lead.lead_metadata["leadme_last_level"], 3)


if __name__ == "__main__":
    unittest.main()
