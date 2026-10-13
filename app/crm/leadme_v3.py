"""Official LeadMe v3 client for verified status and tag writes."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Any, Optional

import httpx
from loguru import logger

from app.config import get_settings


_BASE = "https://api.leadmecms.co.il/v3"
_TIMEOUT_SECONDS = 8.0
_WARNED_DISABLED: dict[str, bool] = {"value": False}

# LeadMe answers HTTP 429 {"message": "Rate limit exceeded"} past 60 v3
# requests per minute (measured 2026-10-13: throttled after exactly 60 calls
# in 5.2s). The limit is per API key, so one process-wide gate covers every
# call site.
#
# This matters more than it looks. A throttled read is indistinguishable from
# "LeadMe has no such lead", and a scan that ignores the limit therefore
# reports most of the customer's leads as missing when every one of them is
# present. That false reading has already been acted on once.
_RATE_LIMIT_PER_MINUTE = 60
_RATE_WINDOW_SECONDS = 60.0
# How long one call may wait for budget. Usually a lead is waiting at the
# other end, so a call that cannot get a slot fails fast and the existing
# retry queue picks the work up later.
_RATE_MAX_WAIT_SECONDS = 2.0

_rate_lock = Lock()
_recent_calls: deque[float] = deque()


def _prune_rate_window(now: float) -> None:
    """Drop call timestamps that have left the window. Caller holds the lock."""
    while _recent_calls and now - _recent_calls[0] >= _RATE_WINDOW_SECONDS:
        _recent_calls.popleft()


def remaining_rate_budget() -> int:
    """Return how many v3 calls may be made right now without waiting."""
    with _rate_lock:
        _prune_rate_window(time.monotonic())
        return max(0, _RATE_LIMIT_PER_MINUTE - len(_recent_calls))


def _reserve_rate_slot() -> bool:
    """Take one slot from the rate budget, waiting briefly for one if needed."""
    deadline = time.monotonic() + _RATE_MAX_WAIT_SECONDS
    while True:
        with _rate_lock:
            now = time.monotonic()
            _prune_rate_window(now)
            if len(_recent_calls) < _RATE_LIMIT_PER_MINUTE:
                _recent_calls.append(now)
                return True
            wait_for = _RATE_WINDOW_SECONDS - (now - _recent_calls[0])
        if time.monotonic() + wait_for > deadline:
            logger.warning(
                "[leadme_v3] rate budget exhausted; skipping a call rather "
                "than waiting {:.0f}s for it", wait_for,
            )
            return False
        time.sleep(min(wait_for, 0.25))


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


def _parse_response(
    resp: httpx.Response, endpoint: str
) -> tuple[str, Optional[dict[str, Any]]]:
    """Return ``(outcome, data)`` for one v3 response.

    The outcome separates the two reasons a call yields nothing:

    * ``"answered"`` -- LeadMe replied and the answer is no. For a lead
      lookup that means the lead genuinely is not there.
    * ``"unavailable"`` -- we never got an answer we can reason about:
      throttled, a server fault, or a body we cannot read.

    Collapsing those two is how a rate limit became a false claim about the
    customer's CRM once already. Only ``get_lead_tags_by_phone`` acts on the
    difference; every write path retries either way.
    """
    if resp.status_code == 429:
        logger.warning(
            "[leadme_v3] {} was rate limited despite the local gate; this "
            "answer says nothing about the lead", endpoint,
        )
        return ("unavailable", None)
    if resp.status_code == 404:
        # Documented for an unknown phone or lead: a real answer.
        logger.info("[leadme_v3] {} returned HTTP 404", endpoint)
        return ("answered", None)
    if resp.status_code != 200:
        logger.warning("[leadme_v3] {} returned HTTP {}", endpoint, resp.status_code)
        return ("unavailable", None)
    try:
        data = resp.json()
    except ValueError:
        logger.warning("[leadme_v3] {} returned non-JSON", endpoint)
        return ("unavailable", None)
    if not isinstance(data, dict):
        logger.warning("[leadme_v3] {} returned a non-object JSON body", endpoint)
        return ("unavailable", None)
    if data.get("result") is not True:
        logger.warning(
            "[leadme_v3] {} rejected request: {}",
            endpoint,
            data.get("message"),
        )
        return ("answered", None)
    return ("answered", data)


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        value_int = int(value)
    except (TypeError, ValueError):
        return None
    return value_int if value_int > 0 else None


def _request(
    endpoint: str, payload: dict[str, Any]
) -> tuple[str, Optional[dict[str, Any]]]:
    """Call a documented v3 POST endpoint and report why it yielded nothing.

    See :func:`_parse_response` for the outcome vocabulary. No rate budget
    and a transport failure are both ``"unavailable"``: neither tells us
    anything about the lead we asked about.
    """
    if not is_v3_available():
        return ("unavailable", None)
    if not _reserve_rate_slot():
        return ("unavailable", None)
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{_BASE}/{endpoint}",
                headers=_headers(),
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.warning("[leadme_v3] {} HTTP error: {}", endpoint, exc)
        return ("unavailable", None)
    return _parse_response(response, endpoint)


def _post(endpoint: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Call a documented v3 POST endpoint with validated JSON."""
    return _request(endpoint, payload)[1]


def _normalize_phone(phone: str) -> str:
    """Normalize the app's E.164-ish Israeli phone to LeadMe local form."""
    normalized = phone.strip().lstrip("+")
    if normalized.startswith("972"):
        return "0" + normalized[3:]
    return normalized


def get_statuses() -> Optional[dict[int, str]]:
    """Return the account's validated status-id-to-title mapping."""
    if not is_v3_available() or not _reserve_rate_slot():
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

    data = _parse_response(response, "getStatuses")[1]
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


def get_lead_status_with_outcome(
    *, phone: Optional[str] = None, lead_id: Optional[int] = None
) -> tuple[str, Optional[dict[str, Any]]]:
    """:func:`get_lead_status`, plus whether LeadMe actually answered.

    Only :func:`get_lead_tags_by_phone` needs this. Everything else retries
    whether the lead was absent or the call never landed, so it uses the
    plainer wrapper below.
    """
    if bool(phone) == bool(lead_id):
        logger.warning("[leadme_v3] getLeadStatus requires exactly one identifier")
        return ("answered", None)

    if phone:
        normalized = _normalize_phone(phone)
        if not normalized:
            return ("answered", None)
        payload: dict[str, Any] = {"phone": normalized}
    else:
        valid_lead_id = _positive_int(lead_id)
        if valid_lead_id is None:
            return ("answered", None)
        payload = {"leadId": valid_lead_id}

    outcome, data = _request("getLeadStatus", payload)
    if data is None:
        return (outcome, None)

    result_lead_id = _positive_int(data.get("leadId"))
    status_id = _positive_int(data.get("status"))
    status_title = data.get("statusTitle")
    if result_lead_id is None or status_id is None or not isinstance(status_title, str):
        logger.warning("[leadme_v3] getLeadStatus response is incomplete")
        return ("unavailable", None)
    return ("answered", {
        "leadId": result_lead_id,
        "crmLeadId": _positive_int(data.get("crmLeadId")),
        "status": status_id,
        "statusTitle": status_title,
    })


def get_lead_status(
    *, phone: Optional[str] = None, lead_id: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """Return a validated status record by phone or LeadMe connection ID."""
    return get_lead_status_with_outcome(phone=phone, lead_id=lead_id)[1]


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
    if valid_lead_id is None or not is_v3_available() or not _reserve_rate_slot():
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

    data = _parse_response(response, "getLeadTags")[1]
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


def get_lead_tags_by_phone(phone: str) -> tuple[str, list[str]]:
    """Return ``("found", tags)``, ``("absent", [])`` or ``("unavailable", [])``.

    This is how the bot learns where a lead actually came from. LeadMe's
    external-interface push fires at record intake, before the source and the
    tags are written, so the payload almost never carries them; v3 is the only
    place the finished record can be read from. There is no per-lead endpoint
    for the source field itself -- ``getLead``, ``getLeadDetails`` and their
    obvious variants all 404 (probed 2026-10-13) -- so the tags are the whole
    of what we can pull.

    The third outcome is the one that matters. At the HTTP level a throttled
    read, an unreachable LeadMe and a lead it has never heard of look alike,
    and reporting any of those as "this lead is not in the CRM" is how an
    outage on either side turns into a wrong conclusion about the customer's
    data. It already has, twice: once from the rate limit, and once when the
    box briefly lost its route to LeadMe mid-scan. ``"absent"`` is returned
    only when LeadMe itself answered.
    """
    if remaining_rate_budget() < 2:
        return ("unavailable", [])
    outcome, status = get_lead_status_with_outcome(phone=phone)
    if status is None:
        return ("absent" if outcome == "answered" else "unavailable", [])
    return ("found", get_lead_tags(status["leadId"]))


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
