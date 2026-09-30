"""Shared engagement-level classifier (LeadMe Level 1/2/3).

Single source of truth for the L1/L2/L3 rule. Used by the webhook opener,
the reply handler, the booking flow, and the bulk reclassification script.

Rules (Omer, 2026-09-29):
    L1 = organic source OR consumed our content. Content-consumed means the
         bot sent a video/webinar AND the lead replied after receiving it.
    L2 = replied to the bot but not L1. Includes booking a call after only
         shallow engagement, price-askers, generic interest, and paid-source
         leads who engaged.
    L3 = not organic and never gave a meaningful reply.

Booking a call is NOT L1 by itself: a booked lead is L2 unless they are also
organic or have consumed content.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.config import get_settings
from app.db.models import Lead, MessageRole


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _parse_iso(value: object) -> Optional[datetime]:
    """Parse an ISO timestamp into a tz-aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_organic(source: object, campaign_id: object) -> bool:
    """Return whether a LeadMe source or campaign is organic."""
    settings = get_settings()
    normalized_source = _normalize(source)
    if normalized_source and normalized_source in {
        _normalize(item) for item in settings.leadme_organic_sources
    }:
        return True
    normalized_campaign = _normalize(campaign_id)
    if normalized_campaign and normalized_campaign in {
        _normalize(item) for item in settings.leadme_organic_campaigns
    }:
        return True
    return False


def is_organic_source(lead: Lead) -> bool:
    """Return whether the lead's recorded LeadMe source/campaign is organic."""
    md = lead.lead_metadata or {}
    return is_organic(md.get("leadme_source"), md.get("leadme_campaign_id"))


def is_content_consumed(lead: Lead) -> bool:
    """Return whether the bot sent content and the lead replied afterwards.

    Content is a video or webinar send, timestamped in ``lead_metadata`` by
    ``send_video`` (``webinar_sent_at`` / ``video_sent_at``). Consumed means
    at least one ``role=user`` message arrived after the earliest content
    send. Requires ``lead.messages`` to be loaded.
    """
    md = lead.lead_metadata or {}
    sent_times = [
        parsed
        for parsed in (
            _parse_iso(md.get("webinar_sent_at")),
            _parse_iso(md.get("video_sent_at")),
        )
        if parsed is not None
    ]
    if not sent_times:
        return False
    content_sent_at = min(sent_times)
    for message in lead.messages or []:
        if message.role != MessageRole.user:
            continue
        created_at = message.created_at
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at > content_sent_at:
            return True
    return False


def compute_engagement_level(
    lead: Lead,
    *,
    user_replied: bool,
    content_consumed: Optional[bool] = None,
    organic: Optional[bool] = None,
) -> int:
    """Return the engagement level (1/2/3) for a lead.

    ``organic`` and ``content_consumed`` default to the module helpers when
    left as ``None``. L1 if organic or content consumed; else L2 if the lead
    replied; else L3.
    """
    if organic is None:
        organic = is_organic_source(lead)
    if content_consumed is None:
        content_consumed = is_content_consumed(lead)
    if organic or content_consumed:
        return 1
    if user_replied:
        return 2
    return 3
