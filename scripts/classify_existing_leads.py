"""Bulk-classify every existing lead into engagement Level 1/2/3 and push
the classification to LeadMe.

Classification rules (deterministic, no LLM needed):

- Level 1 (booked): ``funnel_stage == handed_off`` OR
  ``preferred_call_slot`` is a valid canonical window (9-12 / 12-15 /
  15-18 / any) — the sales team already has a slot.
- Level 2 (replied, no book): has at least one ``role=user`` message in
  the DB history AND does NOT match Level 1.
- Level 3 (never replied): 0 user messages in history.

The push uses ``push_engagement_level`` which is idempotent per
(lead, level). Runs the SAME code path a live lead would hit — so tags,
statuses, and metadata all match what the real-time pipeline produces.

Usage:
    docker exec -e PYTHONPATH=/app propeller_bot \\
        python scripts/classify_existing_leads.py [--commit]

Without ``--commit`` the script only prints what it WOULD do. Add
``--commit`` to actually push to LeadMe.

Extra guards (kept from the eval harness):
- 999-prefix test phones are skipped.
- Leads with ``bot_muted=True`` are skipped (admin took them over).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Optional

from loguru import logger
from sqlalchemy import select

from app.crm.leadme_client import (
    _is_test_phone,
    push_engagement_level,
    push_lead,
)
from app.crm.leadme_delete import _build_client, get_current_status_text
from app.db.models import FunnelStage, Lead, Message, MessageRole
from app.db.session import session_scope


VALID_SLOTS = {"9-12", "12-15", "15-18", "any"}


def _classify(lead: Lead, user_msg_count: int) -> int:
    """Return 1, 2, or 3 per the rules described in the module docstring."""
    md = lead.lead_metadata or {}
    slot = (md.get("preferred_call_slot") or "").strip().lower()
    if lead.funnel_stage == FunnelStage.handed_off or slot in VALID_SLOTS:
        return 1
    if user_msg_count > 0:
        return 2
    return 3


def _describe(lead: Lead, level: int, user_count: int) -> str:
    return (
        f"lead={lead.id} phone={lead.phone!r} name={lead.name!r} "
        f"stage={lead.funnel_stage.value} user_msgs={user_count} -> Level {level}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually push to LeadMe. Without this, dry-run only.",
    )
    parser.add_argument(
        "--only-level", type=int, choices=[1, 2, 3], default=None,
        help="Filter to leads whose computed level equals this.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Push even if leadme_last_level already records this level.",
    )
    parser.add_argument(
        "--only-if-still-new", action="store_true",
        help=(
            "Before pushing, query LeadMe for the lead's current status via "
            "the admin API. Skip any lead whose current status is NOT the "
            "plain 'חדש' (e.g., anything a human already moved to a Level "
            "1/2/3 status or any other status). Requires fresh cookies at "
            "LEADME_COOKIES_PATH."
        ),
    )
    args = parser.parse_args(argv)

    counts: Counter[int] = Counter()
    skipped_test = 0
    skipped_muted = 0
    skipped_already = 0
    skipped_not_new = 0
    skipped_no_lm = 0
    pushed_ok = 0
    pushed_fail = 0

    lm_client = None
    if args.only_if_still_new and args.commit:
        lm_client = _build_client()
        if lm_client is None:
            print("ERROR: --only-if-still-new requires LEADME_COOKIES_PATH "
                  "with a valid cookies file. Aborting.")
            return 2

    with session_scope() as session:
        # Order oldest -> newest so LeadMe sees a stable timeline of updates.
        leads = list(session.execute(
            select(Lead).order_by(Lead.id.asc())
        ).scalars().all())

        for lead in leads:
            if _is_test_phone(lead.phone):
                skipped_test += 1
                continue
            if getattr(lead, "bot_muted", False):
                skipped_muted += 1
                continue

            user_count = session.execute(
                select(Message).where(
                    Message.lead_id == lead.id,
                    Message.role == MessageRole.user,
                )
            ).unique().scalars().all()
            user_count = len(user_count)

            level = _classify(lead, user_count)
            counts[level] += 1

            if args.only_level and level != args.only_level:
                continue

            md = lead.lead_metadata or {}
            already = md.get("leadme_last_level")
            if not args.force and already is not None:
                # Level 1 is always allowed to overwrite (booking can happen
                # after any prior state); Levels 2/3 only if strictly higher.
                if level != 1 and int(already) >= level:
                    skipped_already += 1
                    continue

            if args.only_if_still_new and args.commit and lm_client is not None:
                status_text = get_current_status_text(lead.phone, lm_client)
                if status_text is None:
                    logger.warning(
                        "[classify] no LeadMe row for phone={} -- skipping",
                        lead.phone,
                    )
                    skipped_no_lm += 1
                    continue
                # Anything OTHER than the plain "חדש" bucket = human already
                # touched it (רמה 1/2/3, מעוניין, לא רלוונטי, וכו'). Skip.
                if "רמה" in status_text or "חדש" not in status_text:
                    print(f"SKIP (not חדש) lead={lead.id} phone={lead.phone!r} "
                          f"leadme_status={status_text!r}")
                    skipped_not_new += 1
                    continue

            print(_describe(lead, level, user_count))

            if args.commit:
                try:
                    if args.force:
                        # Bypass push_engagement_level's idempotency guard
                        # so we can re-push after fixing an underlying
                        # config bug (e.g., wrong status IDs).
                        ok = push_lead(
                            lead, note=f"bulk-reclassify (force)", level=level,
                        )
                        if ok:
                            md_new = dict(lead.lead_metadata or {})
                            md_new["leadme_last_level"] = int(level)
                            lead.lead_metadata = md_new
                    else:
                        ok = push_engagement_level(
                            lead, level=level,
                            note=f"bulk-classified from {user_count} msgs",
                        )
                except Exception:
                    logger.exception(
                        "[classify] push failed for lead {}", lead.id,
                    )
                    pushed_fail += 1
                    continue
                if ok:
                    pushed_ok += 1
                else:
                    pushed_fail += 1

    print("\n" + "=" * 60)
    print(f"Would classify: L1={counts[1]}  L2={counts[2]}  L3={counts[3]}")
    print(f"skipped: test={skipped_test} muted={skipped_muted} "
          f"already-classified={skipped_already} "
          f"not-חדש-in-leadme={skipped_not_new} "
          f"not-in-leadme={skipped_no_lm}")
    if args.commit:
        print(f"pushed to LeadMe: ok={pushed_ok} failed={pushed_fail}")
    else:
        print("DRY RUN (no LeadMe pushes). Add --commit to execute.")
    if lm_client is not None:
        try:
            lm_client.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
