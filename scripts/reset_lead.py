"""Hard-delete a lead (and its messages) by phone number so the next
inbound WhatsApp from that number starts a completely fresh conversation.

Useful for manual QA of the opener / warm-up flow without needing the
admin UI. Messages cascade via the ondelete=CASCADE on Message.lead_id.

Usage (inside the bot container):

    docker exec -it propeller_bot python scripts/reset_lead.py 0548897443
    docker exec -it propeller_bot python scripts/reset_lead.py 972548897443
    docker exec -it propeller_bot python scripts/reset_lead.py --all-test  # only phones starting with 999 (eval fixtures)
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.db.models import Lead
from app.db.session import session_scope


def _normalize(raw: str) -> list[str]:
    """Return the set of candidate phone strings we'll try to match."""
    digits = "".join(c for c in raw if c.isdigit())
    variants = {digits}
    if digits.startswith("0"):
        variants.add("972" + digits[1:])
    if digits.startswith("972") and len(digits) > 3:
        variants.add("0" + digits[3:])
    return sorted(variants)


def _delete_where(pred_desc: str, candidates: list[str] | None,
                  all_test: bool) -> int:
    with session_scope() as s:
        q = select(Lead)
        if all_test:
            leads = [l for l in s.execute(q).scalars().all()
                     if (l.phone or "").startswith("999")]
        else:
            leads = [l for l in s.execute(q).scalars().all()
                     if (l.phone or "") in set(candidates or [])]

        if not leads:
            print(f"No leads matched {pred_desc}.")
            return 0

        for l in leads:
            print(f"  deleting lead id={l.id} phone={l.phone} "
                  f"name={l.name or '-'} stage={l.funnel_stage.value}")
            s.delete(l)

    print(f"Deleted {len(leads)} lead(s).")
    return len(leads)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phone", nargs="?", help="Phone (any format).")
    p.add_argument("--all-test", action="store_true",
                   help="Delete all leads whose phone starts with 999 "
                        "(eval-harness fixtures).")
    args = p.parse_args()

    if args.all_test:
        return 0 if _delete_where("--all-test", None, True) >= 0 else 1
    if not args.phone:
        p.error("phone is required unless --all-test is passed")

    candidates = _normalize(args.phone)
    print(f"Trying to match phone variants: {candidates}")
    _delete_where(f"phone in {candidates}", candidates, False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
