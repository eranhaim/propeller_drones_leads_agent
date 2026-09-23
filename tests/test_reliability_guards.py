"""Regression tests for deterministic, dependency-free reliability guards."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.crm.leadme_queue import (
    _is_due,
    _is_expired,
    _merge_ctwa_tag,
    _merge_engagement,
)
from app.webhook.server import _normalize_phone


class PhoneNormalizationTests(unittest.TestCase):
    def test_normalizes_common_israeli_formats(self) -> None:
        self.assertEqual(_normalize_phone("052-123-4567"), "972521234567")
        self.assertEqual(_normalize_phone("+972-052-123-4567"), "972521234567")
        self.assertEqual(_normalize_phone("00972 52 123 4567"), "972521234567")

    def test_rejects_empty_phone(self) -> None:
        self.assertEqual(_normalize_phone("not a phone"), "")


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


if __name__ == "__main__":
    unittest.main()
