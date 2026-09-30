"""CRM integration facade.

Delegates to LeadMe (or any future CRM) so callers in the agent layer keep
using ``mark_ready_for_call`` regardless of which backend is wired up.
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


def mark_ready_for_call(
    lead: Lead,
    note: Optional[str] = None,
    *,
    session: Optional[Session] = None,
) -> bool:
    """Push engagement Level 1 (hottest) to the external CRM (LeadMe).

    Level 1 means organic or content-consumed. Idempotent per lead. Booking
    a call does not imply Level 1 -- the call-window tag is a separate concern
    handled by :func:`mark_call_window`.
    """
    return push_engagement_level(lead, level=1, note=note, session=session)


def mark_engaged_no_book(
    lead: Lead,
    note: Optional[str] = None,
    *,
    session: Optional[Session] = None,
) -> bool:
    """Engagement Level 2: lead replied to the bot but never booked."""
    return push_engagement_level(lead, level=2, note=note, session=session)


def mark_no_reply(
    lead: Lead,
    note: Optional[str] = None,
    *,
    session: Optional[Session] = None,
) -> bool:
    """Engagement Level 3: lead never replied to the opener."""
    return push_engagement_level(lead, level=3, note=note, session=session)


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
