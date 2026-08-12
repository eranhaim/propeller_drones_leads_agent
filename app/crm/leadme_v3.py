"""LeadMe v3 API client.

Replaces the old cookie-based admin endpoints with the official REST API.

Configured via:
    LEADME_API_KEY -- API key from LeadMe CMS settings

Status IDs:
    7326 -- חדש - רמה 1 (booked a call)
    7327 -- חדש - רמה 2 (replied to bot, no booking)
    7328 -- חדש - רמה 3 (no reply at all)
"""

from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger

from app.config import get_settings

_BASE = "https://api.leadmecms.co.il/v3"

LEVEL_STATUS_ID = {
    1: 7326,  # קבע שיחה
    2: 7327,  # ענה לבוט
    3: 7328,  # לא ענה
}


def _headers() -> dict:
    return {
        "LeadMeCMS-API-Key": get_settings().leadme_api_key,
        "Content-Type": "application/json",
    }


def _normalize_phone(phone: str) -> str:
    """Normalize phone to format LeadMe expects.

    Our DB stores phones as '972524859220' (no +).
    LeadMe accepts '0524859220' or '+972524859220'.
    """
    p = phone.strip().lstrip("+")
    if p.startswith("972"):
        p = "0" + p[3:]
    return p


def get_lead_id(phone: str) -> Optional[int]:
    """Look up a lead by phone and return its leadId, or None if not found."""
    normalized = _normalize_phone(phone)
    try:
        with httpx.Client(timeout=8.0) as client:
            req = client.build_request(
                "GET",
                f"{_BASE}/getLeadStatus",
                headers=_headers(),
                json={"phone": normalized},
            )
            resp = client.send(req)
        data = resp.json()
        if data.get("result"):
            return data.get("leadId")
        logger.warning("[leadme_v3] getLeadStatus failed for {}: {}", normalized, data.get("message"))
        return None
    except Exception:
        logger.exception("[leadme_v3] getLeadStatus raised for phone={}", normalized)
        return None


def update_lead_status(lead_id: int, status_id: int) -> bool:
    """Update lead status by leadId. Returns True on success."""
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                f"{_BASE}/updateLeadStatus",
                headers=_headers(),
                json={"leadId": lead_id, "status": status_id},
            )
        data = resp.json()
        if not data.get("result"):
            logger.error("[leadme_v3] updateLeadStatus failed: leadId={} status={} msg={}",
                         lead_id, status_id, data.get("message"))
            return False
        logger.info("[leadme_v3] status updated: leadId={} status={}", lead_id, status_id)
        return True
    except Exception:
        logger.exception("[leadme_v3] updateLeadStatus raised: leadId={}", lead_id)
        return False


def add_lead_tag(lead_id: int, tag: str) -> bool:
    """Add a tag to a lead. Returns True on success."""
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                f"{_BASE}/addLeadTag",
                headers=_headers(),
                json={"leadId": lead_id, "tag": tag},
            )
        data = resp.json()
        if data.get("result"):
            logger.info("[leadme_v3] tag added: leadId={} tag={!r}", lead_id, tag)
            return True
        logger.error("[leadme_v3] addLeadTag failed: leadId={} tag={!r} msg={}",
                     lead_id, tag, data.get("message"))
        return False
    except Exception:
        logger.exception("[leadme_v3] addLeadTag raised: leadId={}", lead_id)
        return False


def get_lead_tags(lead_id: int) -> list[str]:
    """Return a list of tag strings for the given leadId."""
    try:
        with httpx.Client(timeout=8.0) as client:
            req = client.build_request(
                "GET",
                f"{_BASE}/getLeadTags",
                headers=_headers(),
                json={"leadId": lead_id},
            )
            resp = client.send(req)
        data = resp.json()
        if data.get("result"):
            return [t["tag"] for t in (data.get("tags") or []) if "tag" in t]
        return []
    except Exception:
        logger.exception("[leadme_v3] getLeadTags raised for leadId={}", lead_id)
        return []


# Tags that indicate the lead is already sales-ready and should be
# classified as Level 1 immediately (no need to wait for bot booking).
AUTO_LEVEL1_TAGS = frozenset({
    "שיחה נכנסת",
    "אתר הבית",
    "עמוד נחיתה",
})


def check_auto_level1(phone: str) -> bool:
    """Check if a lead has tags that make it auto-Level-1.

    Returns True if the lead has any of the AUTO_LEVEL1_TAGS.
    Returns False if the lead isn't found or has no matching tags.
    """
    if not get_settings().leadme_api_key:
        return False
    lead_id = get_lead_id(phone)
    if not lead_id:
        return False
    tags = get_lead_tags(lead_id)
    matching = AUTO_LEVEL1_TAGS & set(tags)
    if matching:
        logger.info("[leadme_v3] lead {} has auto-L1 tags: {}", phone, matching)
        return True
    return False


def push_level(phone: str, level: int, tag: Optional[str] = None) -> bool:
    """Main entry point: look up lead by phone, set engagement level status,
    and optionally add a tag.

    level: 1 = booked call, 2 = replied, 3 = no reply
    """
    if not get_settings().leadme_api_key:
        logger.debug("[leadme_v3] LEADME_API_KEY not set, skipping push")
        return False

    status_id = LEVEL_STATUS_ID.get(level)
    if not status_id:
        logger.error("[leadme_v3] unknown level={}", level)
        return False

    lead_id = get_lead_id(phone)
    if not lead_id:
        logger.warning("[leadme_v3] lead not found in LeadMe for phone={}", phone)
        return False

    ok = update_lead_status(lead_id, status_id)

    if tag:
        add_lead_tag(lead_id, tag)

    return ok
