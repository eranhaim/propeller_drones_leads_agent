"""CRM integration facade.

Delegates to LeadMe (or any future CRM) so callers in the agent layer keep
using ``mark_engagement`` regardless of which backend is wired up.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.crm.leadme_client import (
    push_call_window_tag,
    push_engagement_level,
    push_lead,
    push_lead_cancellation,
    push_not_relevant,
)
from app.db.models import Lead


def mark_engagement(
    lead: Lead,
    level: int,
    note: Optional[str] = None,
    *,
    session: Optional[Session] = None,
) -> bool:
    """Push the engagement level chosen by ``classify_engagement`` to LeadMe.

    There is deliberately no per-level helper. Every caller classifies first
    and pushes whatever came out, so no code path can decide on its own that
    some event (booking a call, for instance) means Level 1.
    """
    return push_engagement_level(lead, level=level, note=note, session=session)


def mark_not_relevant(
    lead: Lead,
    note: Optional[str] = None,
    *,
    session: Optional[Session] = None,
) -> bool:
    """Set the documented LeadMe ``not relevant`` terminal status."""
    return push_not_relevant(lead, note=note, session=session)


def mark_call_window(
    lead: Lead,
    slot: Optional[str],
    *,
    session: Optional[Session] = None,
) -> bool:
    """Attach the ``חלון · <slot>`` tag for a booked lead.

    Sole owner of the call-window tag. Independent of the engagement level so
    a booked lead carries its window even when its status push is a same-level
    no-op. Idempotent. ``session`` is accepted for parity with the sibling
    ``mark_*`` helpers; v3 tagging itself is sessionless.
    """
    return push_call_window_tag(lead, slot, session=session)


def cancel_ready_for_call(lead: Lead, reason: Optional[str] = None) -> bool:
    """Notify the CRM that a previously-scheduled call was cancelled by
    the lead. Sales sees the note; no auto-delete."""
    return push_lead_cancellation(lead, reason=reason)
