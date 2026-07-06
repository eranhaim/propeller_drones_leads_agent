"""LeadMe CMS client.

Reverse-engineered against LeadMe (Israeli CRM). Two production paths are
supported, selected by env vars:

1) SESSION MODE (LEADME_MODE=session):
   Uses the internal admin endpoint POST /app/leads/leadAction, which is
   the same endpoint the human admin panel uses. Requires an authenticated
   PHP session cookie (LEADME_PHPSESSID) plus a matching CSRF pair
   (LEADME_CSRF_COOKIE / LEADME_CSRF_TOKEN, both equal to the same hex
   string). This is useful for local testing / short-term operation but
   session cookies expire and re-login requires solving reCAPTCHA, so it
   is not a long-term production path.

2) WEBHOOK MODE (LEADME_MODE=webhook, default):
   Posts JSON (or form-encoded, controlled by LEADME_WEBHOOK_ENCODING) to
   LEADME_API_URL with optional auth (Bearer / Token / Basic / Query
   header). Use this once Propeller Drones asks LeadMe support to
   provision a public "external interface" webhook URL for the WhatsApp
   bot (same pattern LeadMe uses for Facebook / TikTok lead ads).

While LEADME_API_URL (webhook mode) and LEADME_PHPSESSID (session mode)
are empty, the client no-ops and just logs -- so nothing changes
behaviorally for the bot until you configure it.

Env vars consumed:
    LEADME_MODE                  session | webhook   (default: webhook)
    LEADME_API_URL               webhook URL (webhook mode)
    LEADME_API_TOKEN             secret token (webhook mode)
    LEADME_AUTH_SCHEME           Bearer | Token | Basic | Query | None
    LEADME_AUTH_QUERY_KEY        query-param name when AUTH_SCHEME == Query
    LEADME_WEBHOOK_ENCODING      json | form  (default: json)
    LEADME_PHPSESSID             PHPSESSID cookie (session mode)
    LEADME_CSRF_COOKIE           csrf_cookie_name value (session mode)
    LEADME_CSRF_TOKEN            same value as CSRF_COOKIE, sent as
                                 csrf_lmcms form field (session mode)
    LEADME_CAMPAIGN_ID           LeadMe campaign_id to attach leads to
                                 (session mode; default: 12277 =
                                 "leads from WhatsApp")
    LEADME_STATUS_ID             LeadMe status_id for handed-off leads
                                 (session mode; default: 1)
    LEADME_READY_STATUS          label to send in the note (both modes)
    LEADME_SOURCE_LABEL          source label in the note (both modes)
    LEADME_FIELD_MAP             JSON dict overriding internal->external
                                 field names (webhook mode only).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.config import get_settings
from app.db.models import Lead

LEADME_BASE = "https://www.leadmecms.co.il"
LEADME_SESSION_ENDPOINT = f"{LEADME_BASE}/app/leads/leadAction"
LEADME_LEAD_FORM_URL = f"{LEADME_BASE}/app/leads/leadForm"

DEFAULT_FIELD_MAP: Dict[str, str] = {
    "phone": "phone",
    "name": "full_name",
    "email": "email",
    "status": "status",
    "note": "comments",
    "source": "source",
    "intent": "intent",
    "industry": "industry",
    "familiarity": "familiarity",
    "funnel_stage": "funnel_stage",
    "preferred_call_slot": "preferred_call_slot",
    "videos_sent": "videos_sent",
}


def _internal_payload(lead: Lead, extra_note: Optional[str]) -> Dict[str, Any]:
    """Normalized internal payload; both modes derive from this."""
    settings = get_settings()
    md = dict(lead.lead_metadata or {})

    note_parts = []
    if extra_note:
        note_parts.append(extra_note)
    if md.get("intent"):
        note_parts.append(f"Intent: {md['intent']}")
    if md.get("industry"):
        note_parts.append(f"Industry: {md['industry']}")
    if md.get("preferred_call_slot"):
        note_parts.append(f"Preferred slot: {md['preferred_call_slot']}")
    if md.get("has_experience") is not None:
        note_parts.append(f"Has experience: {md['has_experience']}")
    if lead.videos_sent:
        note_parts.append(f"Videos sent: {', '.join(lead.videos_sent)}")

    display = (lead.display_name or "").strip()
    if display and " " in display:
        first, last = display.split(" ", 1)
    else:
        first, last = display, ""

    return {
        "phone": lead.phone or "",
        "name": display,
        "firstname": first,
        "lastname": last,
        "email": md.get("email") or "",
        "status": settings.leadme_ready_status,
        "note": " | ".join(note_parts),
        "source": settings.leadme_source_label,
        "intent": md.get("intent") or "",
        "industry": md.get("industry") or "",
        "familiarity": (lead.familiarity_level.value if lead.familiarity_level else ""),
        "funnel_stage": (lead.funnel_stage.value if lead.funnel_stage else ""),
        "preferred_call_slot": md.get("preferred_call_slot") or "",
        "videos_sent": ",".join(lead.videos_sent or []),
    }


def _field_map() -> Dict[str, str]:
    raw = (get_settings().leadme_field_map_json or "").strip()
    if not raw:
        return dict(DEFAULT_FIELD_MAP)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except json.JSONDecodeError:
        logger.warning("LEADME_FIELD_MAP is not valid JSON, using defaults")
    return dict(DEFAULT_FIELD_MAP)


def _apply_auth(
    request_kwargs: Dict[str, Any],
    headers: Dict[str, str],
    params: Dict[str, str],
) -> None:
    settings = get_settings()
    scheme = (settings.leadme_auth_scheme or "").strip()
    token = settings.leadme_api_token or ""
    if not token or scheme.lower() in ("", "none"):
        return
    lower = scheme.lower()
    if lower == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif lower == "token":
        headers["Authorization"] = f"Token {token}"
    elif lower == "basic":
        request_kwargs["auth"] = ("api", token)
    elif lower == "query":
        params[settings.leadme_auth_query_key] = token
    else:
        headers["Authorization"] = f"{scheme} {token}"


# ---------- webhook mode ---------------------------------------------------


def _push_via_webhook(lead: Lead, payload: Dict[str, Any]) -> bool:
    settings = get_settings()
    url = settings.leadme_api_url.strip()
    if not url:
        logger.info(
            "[LeadMe STUB] webhook not configured. Would send {} -> LeadMe. payload={}",
            lead.phone, payload,
        )
        return True

    fmap = _field_map()
    external: Dict[str, Any] = {}
    for internal_key, ext_key in fmap.items():
        val = payload.get(internal_key, "")
        if val == "" or val is None:
            continue
        external[ext_key] = val

    headers = {"Accept": "application/json"}
    params: Dict[str, str] = {}
    req_kwargs: Dict[str, Any] = {}
    _apply_auth(req_kwargs, headers, params)

    encoding = (get_settings_or_env("LEADME_WEBHOOK_ENCODING") or "json").lower()
    try:
        if encoding == "form":
            resp = httpx.post(
                url, data=external, headers=headers,
                params=params or None, timeout=15.0, **req_kwargs,
            )
        else:
            headers["Content-Type"] = "application/json"
            resp = httpx.post(
                url, json=external, headers=headers,
                params=params or None, timeout=15.0, **req_kwargs,
            )
    except httpx.HTTPError as e:
        logger.exception("[LeadMe webhook] HTTP error for {}: {}", lead.phone, e)
        return False

    if 200 <= resp.status_code < 300:
        logger.info(
            "[LeadMe webhook] pushed lead {} -> {}", lead.phone, resp.status_code
        )
        return True
    logger.error(
        "[LeadMe webhook] push failed for {}: {} {}",
        lead.phone, resp.status_code, resp.text[:400],
    )
    return False


# ---------- session mode ---------------------------------------------------


def _push_via_session(lead: Lead, payload: Dict[str, Any]) -> bool:
    """POST to the internal /app/leads/leadAction using an existing
    authenticated PHP session. Field set was reverse-engineered by
    observing what the admin panel's own Save button submits."""
    phpsessid = get_settings_or_env("LEADME_PHPSESSID") or ""
    csrf_cookie = get_settings_or_env("LEADME_CSRF_COOKIE") or ""
    csrf_token = get_settings_or_env("LEADME_CSRF_TOKEN") or ""
    if not (phpsessid and csrf_cookie and csrf_token):
        logger.info(
            "[LeadMe session STUB] session not configured. Would POST {} to {}."
            " payload={}",
            lead.phone, LEADME_SESSION_ENDPOINT, payload,
        )
        return True

    campaign_id = get_settings_or_env("LEADME_CAMPAIGN_ID") or "12277"
    status_id = get_settings_or_env("LEADME_STATUS_ID") or "1"

    form = {
        "csrf_lmcms": csrf_token,
        "campaignId": campaign_id,
        "status": status_id,
        "firstname": payload["firstname"] or lead.phone,
        "lastname": payload["lastname"],
        "phone": payload["phone"],
        "email": payload["email"],
        "businessname": "",
        "address": "",
        "appartment": "",
        "city": "",
        "zipCode": "",
        "comment": payload["note"],
        "tags": get_settings_or_env("LEADME_SOURCE_LABEL")
                or "WhatsApp Bot",
    }

    cookies = {
        "PHPSESSID": phpsessid,
        "csrf_cookie_name": csrf_cookie,
    }
    headers = {
        "Referer": LEADME_LEAD_FORM_URL,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
    }

    try:
        resp = httpx.post(
            LEADME_SESSION_ENDPOINT, data=form, cookies=cookies,
            headers=headers, timeout=20.0, follow_redirects=False,
        )
    except httpx.HTTPError as e:
        logger.exception("[LeadMe session] HTTP error for {}: {}", lead.phone, e)
        return False

    # 200 or 302 with Location -> success. Session expired -> redirect to /login.
    location = resp.headers.get("location", "")
    if "/login" in location:
        logger.error("[LeadMe session] session expired -- Location={}", location)
        return False
    if resp.status_code in (200, 302, 303):
        logger.info(
            "[LeadMe session] pushed lead {} -> {} loc={}",
            lead.phone, resp.status_code, location,
        )
        return True
    logger.error(
        "[LeadMe session] push failed for {}: {} {}",
        lead.phone, resp.status_code, resp.text[:400],
    )
    return False


def get_settings_or_env(name: str) -> str:
    """Read an override that isn't part of the pydantic Settings model."""
    import os
    return os.environ.get(name, "") or ""


def push_lead(lead: Lead, note: Optional[str] = None) -> bool:
    """Create or update the lead in LeadMe using the configured mode."""
    payload = _internal_payload(lead, note)
    mode = (get_settings_or_env("LEADME_MODE") or "webhook").lower()
    if mode == "session":
        return _push_via_session(lead, payload)
    return _push_via_webhook(lead, payload)
