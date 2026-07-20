"""CRM integration facade.

Delegates to LeadMe (or any future CRM) so callers in the agent layer keep
using ``mark_ready_for_call`` regardless of which backend is wired up.
"""

from __future__ import annotations

from typing import Optional

from app.crm.leadme_client import (
    push_engagement_level,
    push_lead,
    push_lead_cancellation,
)
from app.db.models import Lead


def mark_ready_for_call(lead: Lead, note: Optional[str] = None, slot: Optional[str] = None) -> bool:
    """Push the ``ready_for_call`` status to the external CRM (LeadMe).

    This is engagement Level 1 (booked a call). Idempotent per lead.
    """
    return push_engagement_level(lead, level=1, note=note, slot=slot)


def mark_engaged_no_book(lead: Lead, note: Optional[str] = None) -> bool:
    """Engagement Level 2: lead replied to the bot but never booked."""
    return push_engagement_level(lead, level=2, note=note)


def mark_no_reply(lead: Lead, note: Optional[str] = None) -> bool:
    """Engagement Level 3: lead never replied to the opener."""
    return push_engagement_level(lead, level=3, note=note)


def cancel_ready_for_call(lead: Lead, reason: Optional[str] = None) -> bool:
    """Notify the CRM that a previously-scheduled call was cancelled by
    the lead. Sales sees the note; no auto-delete."""
    return push_lead_cancellation(lead, reason=reason)
