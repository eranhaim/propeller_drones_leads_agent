"""Re-classify the existing lead base under the current engagement rules.

Classification is the shared rule in ``app.crm.levels.classify_engagement``
-- the same one the live pipeline uses -- so a bulk run and the real-time
flow can never diverge. See ``docs/leadme_integration.md`` for what puts a
lead at Level 1, 2 or 3.

The script runs in two phases:

1. **Report** (always). Classifies every lead and prints the before/after
   distribution across Levels 1/2/3. Writes nothing, reads nothing from
   LeadMe. "Before" is ``lead_metadata["leadme_last_level"]`` -- the level
   this bot last pushed, which is not necessarily what the sales team sees
   today if someone moved the lead by hand in LeadMe.
2. **Push** (only with ``--commit``). Sends the new level to LeadMe through
   ``push_engagement_level``, the same idempotent call the live pipeline
   uses, so statuses, tags and metadata match what a real conversation
   produces.

The call-window tag (``חלון · <slot>``) is attached only for booked leads
(``funnel_stage == handed_off`` with a real slot) via ``mark_call_window``
-- the single owner of that tag -- independent of the engagement level.

Usage:
    docker exec -e PYTHONPATH=/app propeller_bot \\
        python scripts/classify_existing_leads.py [--commit]

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

from app.crm.client import mark_call_window, mark_engagement
from app.crm.leadme_client import _is_test_phone
from app.crm.leadme_v3 import get_lead_status, is_v3_available
from app.crm.levels import classify_engagement
from app.db.models import FunnelStage, Lead
from app.db.session import session_scope


_MANAGED_STATUS_TITLES = {
    "חדש",
    "חדש - רמה 1",
    "חדש - רמה 2",
    "חדש - רמה 3",
}

_LEVELS = (1, 2, 3)


def _recorded_level(lead: Lead) -> Optional[int]:
    """The level this bot last pushed for the lead, if any."""
    recorded = (lead.lead_metadata or {}).get("leadme_last_level")
    try:
        return int(recorded)
    except (TypeError, ValueError):
        return None


def _wants_call_window_tag(lead: Lead) -> bool:
    slot = (lead.lead_metadata or {}).get("preferred_call_slot")
    return (
        lead.funnel_stage == FunnelStage.handed_off
        and bool(slot)
        and slot not in ("any", "none")
    )


def _print_distribution(
    rows: list[tuple[Optional[int], int, str]], skipped: Counter[str]
) -> None:
    before = Counter(row[0] for row in rows)
    after = Counter(row[1] for row in rows)

    print("\n" + "=" * 64)
    print("Engagement level distribution")
    print("-" * 64)
    print(f"{'':<12}{'before':>10}{'after':>10}{'change':>12}")
    for level in _LEVELS:
        delta = after[level] - before[level]
        print(
            f"{'Level ' + str(level):<12}{before[level]:>10}{after[level]:>10}"
            f"{delta:>+12}"
        )
    print(f"{'never set':<12}{before[None]:>10}{'-':>10}{'':>12}")
    print(f"{'total':<12}{len(rows):>10}{len(rows):>10}")

    print("\nWhy each lead landed where it did (after):")
    for reason, count in Counter(row[2] for row in rows).most_common():
        print(f"  {count:>6}  {reason}")

    print("\nMovement (before -> after):")
    moves = Counter((row[0], row[1]) for row in rows)
    for (was, now), count in sorted(
        moves.items(), key=lambda item: (-item[1], str(item[0]))
    ):
        label = "never set" if was is None else f"Level {was}"
        marker = "" if was == now else "  <-- changes"
        print(f"  {count:>6}  {label} -> Level {now}{marker}")

    if skipped:
        print("\nSkipped:")
        for reason, count in skipped.most_common():
            print(f"  {count:>6}  {reason}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually push to LeadMe. Without this, report only.",
    )
    parser.add_argument(
        "--only-level", type=int, choices=list(_LEVELS), default=None,
        help="Push only leads whose new level equals this. Report is unaffected.",
    )
    parser.add_argument(
        "--only-managed-statuses", action="store_true",
        help=(
            "Before pushing, query LeadMe v3 and skip any status outside "
            "חדש / חדש - רמה 1 / חדש - רמה 2 / חדש - רמה 3. This preserves "
            "sales-team statuses outside the bot-managed level flow."
        ),
    )
    args = parser.parse_args(argv)

    if args.only_managed_statuses and not is_v3_available():
        print("ERROR: --only-managed-statuses requires LeadMe v3 API access.")
        return 2

    rows: list[tuple[Optional[int], int, str]] = []
    skipped: Counter[str] = Counter()
    pushed_ok = 0
    pushed_fail = 0

    with session_scope() as session:
        # Oldest -> newest so LeadMe sees a stable timeline of updates.
        leads = list(
            session.execute(select(Lead).order_by(Lead.id.asc())).scalars().all()
        )

        for lead in leads:
            if _is_test_phone(lead.phone):
                skipped["test phone"] += 1
                continue
            if getattr(lead, "bot_muted", False):
                skipped["bot muted (admin took over)"] += 1
                continue

            decision = classify_engagement(lead, lead.messages)
            if decision.level is None:
                skipped[f"outside the numeric levels ({decision.reason})"] += 1
                continue

            rows.append((_recorded_level(lead), decision.level, decision.reason))

            if not args.commit:
                continue
            if args.only_level and decision.level != args.only_level:
                continue

            if args.only_managed_statuses:
                remote = get_lead_status(phone=lead.phone)
                if remote is None:
                    logger.warning(
                        "[classify] no LeadMe row -- skipping lead={}", lead.id,
                    )
                    skipped["no LeadMe row"] += 1
                    continue
                if remote["statusTitle"].strip() not in _MANAGED_STATUS_TITLES:
                    skipped["LeadMe status outside the managed levels"] += 1
                    continue

            try:
                ok = mark_engagement(
                    lead,
                    decision.level,
                    note=f"bulk-reclassify; classification={decision.reason}",
                    session=session,
                )
            except Exception:
                logger.exception("[classify] push failed for lead {}", lead.id)
                pushed_fail += 1
                continue

            if ok:
                if _wants_call_window_tag(lead):
                    mark_call_window(
                        lead,
                        (lead.lead_metadata or {})["preferred_call_slot"],
                        session=session,
                    )
                pushed_ok += 1
            else:
                pushed_fail += 1

    _print_distribution(rows, skipped)

    print()
    if args.commit:
        print(f"Pushed to LeadMe: ok={pushed_ok} failed={pushed_fail}")
    else:
        print("REPORT ONLY -- nothing was written. Add --commit to push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
