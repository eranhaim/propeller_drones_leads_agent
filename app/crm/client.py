"""CRM integration facade.

Delegates to LeadMe (or any future CRM) so callers in the agent layer keep
using ``mark_ready_for_call`` regardless of which backend is wired up.
"""

from __future__ import annotations

from typing import Optional

from app.crm.leadme_client import push_lead, push_lead_cancellation
from app.db.models import Lead


def mark_ready_for_call(lead: Lead, note: Optional[str] = None) -> bool:
    """Push the ``ready_for_call`` status to the external CRM (LeadMe)."""
    return push_lead(lead, note=note)


def cancel_ready_for_call(lead: Lead, reason: Optional[str] = None) -> bool:
    """Notify the CRM that a previously-scheduled call was cancelled by
    the lead. Sales sees the note; no auto-delete."""
    return push_lead_cancellation(lead, reason=reason)
