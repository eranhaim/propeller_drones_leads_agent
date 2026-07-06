"""LeadMe CMS client.

Config-driven so we can plug in the real endpoint the moment we learn it
from the recon session. Until ``LEADME_API_URL`` is set the client no-ops
(logs only) so tests / staging keep working.

Env vars consumed:
    LEADME_API_URL          full endpoint that creates or updates a lead
                            (e.g. https://www.leadmecms.co.il/api/lead/insert)
    LEADME_API_TOKEN        secret token
    LEADME_AUTH_SCHEME      Bearer | Token | Basic | Query | None
    LEADME_AUTH_QUERY_KEY   query-param name when AUTH_SCHEME == Query
    LEADME_READY_STATUS     status value we want the lead to become when
                            it is handed off to sales (default: ready_for_call)
    LEADME_SOURCE_LABEL     value to send in the `source` field
    LEADME_FIELD_MAP        JSON dict mapping internal->LeadMe field names.
                            Only listed keys are sent. Supported internal keys:
                              phone, name, email, status, note, source,
                              intent, industry, familiarity, funnel_stage,
                              preferred_call_slot, videos_sent.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.config import get_settings
from app.db.models import Lead

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


def _build_payload(lead: Lead, extra_note: Optional[str]) -> Dict[str, Any]:
    settings = get_settings()
    fmap = _field_map()
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

    internal: Dict[str, Any] = {
        "phone": lead.phone,
        "name": lead.display_name or "",
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

    payload: Dict[str, Any] = {}
    for internal_key, ext_key in fmap.items():
        val = internal.get(internal_key, "")
        if val == "" or val is None:
            continue
        payload[ext_key] = val
    return payload


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
    if scheme.lower() == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif scheme.lower() == "token":
        headers["Authorization"] = f"Token {token}"
    elif scheme.lower() == "basic":
        request_kwargs["auth"] = ("api", token)
    elif scheme.lower() == "query":
        params[settings.leadme_auth_query_key] = token
    else:
        headers["Authorization"] = f"{scheme} {token}"


def push_lead(lead: Lead, note: Optional[str] = None) -> bool:
    """Create or update the lead in LeadMe. Returns True on 2xx or when the
    client is not configured (stub mode)."""
    settings = get_settings()
    url = settings.leadme_api_url.strip()
    payload = _build_payload(lead, note)

    if not url:
        logger.info(
            "[LeadMe STUB] Not configured. Would send {} -> LeadMe. payload={}",
            lead.phone, payload,
        )
        return True

    headers: Dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    params: Dict[str, str] = {}
    req_kwargs: Dict[str, Any] = {}
    _apply_auth(req_kwargs, headers, params)

    try:
        resp = httpx.post(
            url, json=payload, headers=headers, params=params or None,
            timeout=15.0, **req_kwargs,
        )
    except httpx.HTTPError as e:
        logger.exception("[LeadMe] HTTP error pushing lead {}: {}", lead.phone, e)
        return False

    if 200 <= resp.status_code < 300:
        logger.info("[LeadMe] pushed lead {} -> {}", lead.phone, resp.status_code)
        return True
    logger.error(
        "[LeadMe] push failed for {}: {} {}",
        lead.phone, resp.status_code, resp.text[:400],
    )
    return False
