"""Official LeadMe v3 client for verified status and tag writes."""

from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

from app.config import get_settings


_BASE = "https://api.leadmecms.co.il/v3"
_TIMEOUT_SECONDS = 8.0
_WARNED_DISABLED: dict[str, bool] = {"value": False}


def _is_v3_enabled() -> bool:
    """Return the v3 kill switch and log its disabled state once."""
    enabled = get_settings().leadme_v3_enabled
    if not enabled and not _WARNED_DISABLED["value"]:
        logger.warning("[leadme_v3] LEADME_V3_ENABLED=false; v3 writes are disabled")
        _WARNED_DISABLED["value"] = True
    return enabled


def is_v3_available() -> bool:
    """Return whether v3 has both its key and explicit enable switch."""
    return _is_v3_enabled() and bool(get_settings().leadme_api_key.strip())


def _headers() -> dict[str, str]:
    return {
        "LeadMeCMS-API-Key": get_settings().leadme_api_key,
        "Content-Type": "application/json",
    }


def _parse_response(resp: httpx.Response, endpoint: str) -> Optional[dict[str, Any]]:
    """Return a documented successful JSON response, or ``None``."""
    if resp.status_code != 200:
        logger.warning("[leadme_v3] {} returned HTTP {}", endpoint, resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("[leadme_v3] {} returned non-JSON", endpoint)
        return None
    if not isinstance(data, dict):
        logger.warning("[leadme_v3] {} returned a non-object JSON body", endpoint)
        return None
    if data.get("result") is not True:
        logger.warning(
            "[leadme_v3] {} rejected request: {}",
            endpoint,
            data.get("message"),
        )
        return None
    return data


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        value_int = int(value)
    except (TypeError, ValueError):
        return None
    return value_int if value_int > 0 else None


def _post(endpoint: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Call a documented v3 POST endpoint with validated JSON."""
    if not is_v3_available():
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{_BASE}/{endpoint}",
                headers=_headers(),
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.warning("[leadme_v3] {} HTTP error: {}", endpoint, exc)
        return None
    return _parse_response(response, endpoint)


def _normalize_phone(phone: str) -> str:
    """Normalize the app's E.164-ish Israeli phone to LeadMe local form."""
    normalized = phone.strip().lstrip("+")
    if normalized.startswith("972"):
        return "0" + normalized[3:]
    return normalized


def get_statuses() -> Optional[dict[int, str]]:
    """Return the account's validated status-id-to-title mapping."""
    if not is_v3_available():
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            response = client.get(
                f"{_BASE}/getStatuses",
                headers={"LeadMeCMS-API-Key": get_settings().leadme_api_key},
            )
    except httpx.HTTPError as exc:
        logger.warning("[leadme_v3] getStatuses HTTP error: {}", exc)
        return None

    data = _parse_response(response, "getStatuses")
    statuses = data.get("statuses") if data else None
    if not isinstance(statuses, list):
        logger.warning("[leadme_v3] getStatuses response has no status list")
        return None

    mapping: dict[int, str] = {}
    for item in statuses:
        if not isinstance(item, dict):
            logger.warning("[leadme_v3] getStatuses contains an invalid status")
            return None
        status_id = _positive_int(item.get("id"))
        title = item.get("title")
        if status_id is None or not isinstance(title, str) or not title.strip():
            logger.warning("[leadme_v3] getStatuses contains an incomplete status")
            return None
        if status_id in mapping:
            logger.warning("[leadme_v3] getStatuses contains duplicate id={}", status_id)
            return None
        mapping[status_id] = title.strip()
    return mapping


def get_lead_status(
    *, phone: Optional[str] = None, lead_id: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """Return a validated status record by phone or LeadMe connection ID."""
    if bool(phone) == bool(lead_id):
        logger.warning("[leadme_v3] getLeadStatus requires exactly one identifier")
        return None

    if phone:
        normalized = _normalize_phone(phone)
        if not normalized:
            return None
        payload: dict[str, Any] = {"phone": normalized}
    else:
        valid_lead_id = _positive_int(lead_id)
        if valid_lead_id is None:
            return None
        payload = {"leadId": valid_lead_id}

    data = _post("getLeadStatus", payload)
    if data is None:
        return None

    result_lead_id = _positive_int(data.get("leadId"))
    status_id = _positive_int(data.get("status"))
    status_title = data.get("statusTitle")
    if result_lead_id is None or status_id is None or not isinstance(status_title, str):
        logger.warning("[leadme_v3] getLeadStatus response is incomplete")
        return None
    return {
        "leadId": result_lead_id,
        "crmLeadId": _positive_int(data.get("crmLeadId")),
        "status": status_id,
        "statusTitle": status_title,
    }


def get_lead_id(phone: str) -> Optional[int]:
    """Look up and return a LeadMe connection ID by phone."""
    status = get_lead_status(phone=phone)
    return status["leadId"] if status else None


def update_lead_status(lead_id: int, status_id: int) -> bool:
    """Set a status and verify it with a fresh ``getLeadStatus`` read."""
    valid_lead_id = _positive_int(lead_id)
    valid_status_id = _positive_int(status_id)
    if valid_lead_id is None or valid_status_id is None:
        logger.warning("[leadme_v3] updateLeadStatus got an invalid identifier")
        return False
    if _post(
        "updateLeadStatus",
        {"leadId": valid_lead_id, "status": valid_status_id},
    ) is None:
        return False

    confirmed = get_lead_status(lead_id=valid_lead_id)
    if confirmed is None or confirmed["status"] != valid_status_id:
        logger.warning(
            "[leadme_v3] updateLeadStatus was not confirmed for leadId={} status={}",
            valid_lead_id,
            valid_status_id,
        )
        return False
    logger.info(
        "[leadme_v3] confirmed status update: leadId={} status={}",
        valid_lead_id,
        valid_status_id,
    )
    return True


def get_lead_tags(lead_id: int) -> list[str]:
    """Return a validated list of tags assigned to a LeadMe connection.

    LeadMe's published v3 document currently says POST, but the live account
    returns 405 to that method and accepts the legacy GET JSON-body request.
    Keep this compatibility path until LeadMe fixes the documented endpoint.
    """
    valid_lead_id = _positive_int(lead_id)
    if valid_lead_id is None or not is_v3_available():
        return []
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            request = client.build_request(
                "GET",
                f"{_BASE}/getLeadTags",
                headers=_headers(),
                json={"leadId": valid_lead_id},
            )
            response = client.send(request)
    except httpx.HTTPError as exc:
        logger.warning("[leadme_v3] getLeadTags HTTP error: {}", exc)
        return []

    data = _parse_response(response, "getLeadTags")
    if data is None:
        return []
    tags = data.get("tags")
    if not isinstance(tags, list):
        logger.warning("[leadme_v3] getLeadTags response has no tag list")
        return []

    values: list[str] = []
    for item in tags:
        if not isinstance(item, dict) or not isinstance(item.get("tag"), str):
            logger.warning("[leadme_v3] getLeadTags contains an invalid tag")
            return []
        values.append(item["tag"])
    return values


def add_lead_tag(lead_id: int, tag: str) -> bool:
    """Add a tag and confirm it with a fresh ``getLeadTags`` read."""
    valid_lead_id = _positive_int(lead_id)
    tag = tag.strip()
    if valid_lead_id is None or not tag:
        return False
    if _post("addLeadTag", {"leadId": valid_lead_id, "tag": tag}) is None:
        return False
    if tag not in get_lead_tags(valid_lead_id):
        logger.warning(
            "[leadme_v3] addLeadTag was not confirmed for leadId={} tag={!r}",
            valid_lead_id,
            tag,
        )
        return False
    logger.info("[leadme_v3] confirmed tag add: leadId={} tag={!r}", valid_lead_id, tag)
    return True


AUTO_LEVEL1_TAGS = frozenset({"שיחה נכנסת", "אתר הבית", "עמוד נחיתה"})


def check_auto_level1(phone: str) -> bool:
    """Return whether the lead has a tag that marks it Level 1."""
    if not is_v3_available():
        return False
    lead_id = get_lead_id(phone)
    if not lead_id:
        return False
    return bool(AUTO_LEVEL1_TAGS & set(get_lead_tags(lead_id)))


def status_id_for_level(level: int) -> Optional[int]:
    """Return the deployment-configured status ID for an engagement level."""
    settings = get_settings()
    configured = {
        1: settings.leadme_status_level_1,
        2: settings.leadme_status_level_2,
        3: settings.leadme_status_level_3,
    }.get(level, "")
    return _positive_int(configured)


def level_for_status_id(status_id: int) -> Optional[int]:
    """Return the engagement level for a configured LeadMe status ID."""
    valid_status_id = _positive_int(status_id)
    if valid_status_id is None:
        return None
    for level in (1, 2, 3):
        if status_id_for_level(level) == valid_status_id:
            return level
    return None


def push_level(phone: str, level: int, tag: Optional[str] = None) -> bool:
    """Resolve a lead, write its configured level, and verify every write."""
    if not is_v3_available():
        return False
    status_id = status_id_for_level(level)
    if status_id is None:
        logger.error("[leadme_v3] no configured status for level={}", level)
        return False
    lead_id = get_lead_id(phone)
    if lead_id is None:
        logger.info("[leadme_v3] lead not found for a status update")
        return False
    if not update_lead_status(lead_id, status_id):
        return False
    return not tag or add_lead_tag(lead_id, tag)
