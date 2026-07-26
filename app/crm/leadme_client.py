"""LeadMe CMS client -- ADMIN-ONLY path.

Historical note: LeadMe exposes a public "supplier" API (``/supplier/insert``
and ``/supplier/update``) intended for lead-source integrations (Facebook,
TikTok, etc.). On this account BOTH endpoints act as an UPSERT: when the
phone can't be resolved inside a supplier-linked campaign, LeadMe silently
creates a duplicate lead in the supplier's default campaign (id 12277 =
"הוסרו מ-Whatsapp"). That's the "leads keep leaking into the wrong
campaign" bug the customer reported. To make it structurally impossible
for us to reintroduce that bug, all supplier-API code has been DELETED
from this module. Do NOT reintroduce ``httpx.post`` calls to any
``https://api.leadmecms.co.il/supplier/...`` URL. Everything the bot does
now goes through the internal admin endpoints using session cookies:

    POST /app/leads/changeLeadsStatus   -- change status pill
    POST /app/ajax/addLeadTag           -- attach engagement tag
    (see :mod:`app.crm.leadme_delete`   -- delete + phone-lookup)

The CTWA race
-------------
Facebook / TikTok leads reach the bot BEFORE LeadMe's own supplier
sync updates their DB. Historical behaviour was to retry the phone
lookup 4 times over 30 seconds and then give up forever, silently
dropping the push. That produced 40/44 dropped pushes in one 48h
window -- from the customer's perspective, "the LeadMe integration
randomly breaks".

The current behaviour is: try 2 fast in-request attempts (~10s total),
then enqueue the desired action in :mod:`app.crm.leadme_queue`, which
is drained by the follow-up scheduler every few minutes for up to 60
minutes. Only after the queue fully expires do we log a loud ERROR
and give up. Cross-restart safe (state lives on ``lead.lead_metadata``).

Env vars still consumed:
    LEADME_STATUS_LEVEL_1/2/3  -- numeric status ids for engagement tiers
    LEADME_STATUS_ID           -- fallback for level 1 if the tier var is empty
    LEADME_INSERT_MODE=never   -- kill-switch (skip all LeadMe pushes)
    LEADME_TEST_MODE           -- log-only, no HTTP calls
"""

from __future__ import annotations

import time
from typing import Optional

import httpx
from loguru import logger

from app.config import get_settings
from app.db.models import Lead


def _is_test_phone(phone: Optional[str]) -> bool:
    """Return True for synthetic phones used by the eval harness.

    Any push for a phone that starts with the `999` prefix is a test-lead
    push that must NEVER reach LeadMe -- the eval harness churns dozens
    of them per run and they were showing up in the customer's
    'הוסרו מ-whatsapp' trash campaign because LeadMe dedupes on phone
    and upserts previously-trashed numbers back into the trash campaign.
    """
    p = (phone or "").strip()
    return p.startswith("999")


# Reserved for backwards compatibility. Historically we pushed engagement
# level tags (רמה 1/2/3) alongside the status pill; the customer asked
# us to drop those on 2026-07-22 -- the status pill alone carries the
# engagement signal. Kept as an empty dict so no import site breaks.
LEVEL_TAGS: dict[int, str] = {}


# LeadMe campaign id for the "trash" bucket the bot must never leak into.
# We assert on lead rows and log loudly if a bot push ever ends up here.
BANNED_LEAKY_CAMPAIGN_ID = "12277"
BANNED_LEAKY_CAMPAIGN_NAME = "הוסרו מ-Whatsapp"


# How many fast, in-request retries we do before handing off to the
# durable queue. Short by design -- the queue picks up the slack for
# the CTWA race (see module docstring).
_INREQUEST_RETRIES = 2
_INREQUEST_WAIT_SEC = 5  # first wait; second wait doubles this.


def _status_id_for_level(level: int) -> str:
    settings = get_settings()
    return {
        1: (settings.leadme_status_level_1 or settings.leadme_status_id or "").strip(),
        2: (settings.leadme_status_level_2 or "").strip(),
        3: (settings.leadme_status_level_3 or "").strip(),
    }.get(level, "")


def push_lead(
    lead: Lead,
    note: Optional[str] = None,
    level: int = 1,
) -> bool:
    """Sync an engagement change to LeadMe -- admin-only path.

    Flow:
    1. Guards (test mode / banned phones / kill-switch / no phone).
    2. Try 2 fast in-request phone lookups (~10s total).
    3. If found -> do the admin push (status + optional slot tag) and
       return the boolean success.
    4. If not found -> enqueue the desired action on
       ``lead.lead_metadata['leadme_push_pending']`` (see
       :mod:`app.crm.leadme_queue`) and return True. A background
       scheduler tick will drain the queue.

    ``level`` picks the engagement status:
        1 = booked, 2 = replied but no booking, 3 = never replied.

    The slot to tag (``חלון · <slot>``) is read from
    ``lead.lead_metadata['preferred_call_slot']``. Only pushed for
    level=1 (bookings); levels 2/3 don't carry a slot.
    """
    import re as _re
    from app.crm.leadme_delete import _build_client, get_row_by_phone
    from app.crm import leadme_queue

    settings = get_settings()

    if settings.leadme_test_mode:
        logger.info(
            "[LeadMe TEST_MODE] skipping push_lead for {} (test mode on)",
            lead.phone,
        )
        return True
    if _is_test_phone(lead.phone):
        logger.warning(
            "[LeadMe] REFUSING push for test-prefix phone {} -- if this is "
            "a real lead, remove the 999 prefix.",
            lead.phone,
        )
        return True

    mode = (settings.leadme_insert_mode or "update-only").strip().lower()
    if mode == "never":
        logger.info("[LeadMe] insert_mode=never, skipping push for {}",
                    lead.phone)
        return True

    if not (lead.phone or "").strip():
        logger.info(
            "[LeadMe] skipping push for lead {} -- no phone number", lead.id,
        )
        return True

    slot = (lead.lead_metadata or {}).get("preferred_call_slot")

    client = _build_client()
    if client is None:
        logger.warning(
            "[LeadMe] no admin cookies configured; queueing push for lead "
            "{} (level={}, slot={}). Refresh cookies via the /admin panel.",
            lead.phone, level, slot,
        )
        leadme_queue.enqueue_engagement(lead, level=level, slot=slot, note=note)
        return True

    try:
        row = None
        for attempt in range(_INREQUEST_RETRIES):
            row = get_row_by_phone(lead.phone, client)
            if row is not None:
                break
            if attempt < _INREQUEST_RETRIES - 1:
                wait = _INREQUEST_WAIT_SEC * (attempt + 1)
                logger.info(
                    "[LeadMe] phone {} not found yet (in-request attempt "
                    "{}/{}), waiting {}s...",
                    lead.phone, attempt + 1, _INREQUEST_RETRIES, wait,
                )
                time.sleep(wait)

        if row is None:
            # CTWA race: LeadMe supplier sync hasn't landed yet. Hand
            # off to the durable queue instead of dropping.
            logger.info(
                "[LeadMe] phone {} not visible in LeadMe yet -- enqueueing "
                "for background retry (level={}, slot={})",
                lead.phone, level, slot,
            )
            leadme_queue.enqueue_engagement(lead, level=level, slot=slot, note=note)
            return True

        # row layout (see leadme_delete.py):
        #   [checkbox_html, id, name, phone, campaign, status_html, ...]
        lc_id = str(row[1]).strip() if len(row) > 1 else ""
        campaign = ""
        if len(row) > 4 and isinstance(row[4], str):
            campaign = _re.sub(r"<[^>]+>", " ", row[4])
            campaign = _re.sub(r"\s+", " ", campaign).strip()

        # HARD GUARD: if the ONLY visible row for this phone lives in the
        # banned "trash" campaign 12277, do NOT push. Touching it would
        # only reinforce a bad state. Bark loudly so operators can move
        # the lead into a real campaign.
        if (BANNED_LEAKY_CAMPAIGN_ID in (row[0] or "")
                or campaign == BANNED_LEAKY_CAMPAIGN_NAME):
            logger.error(
                "[LeadMe SAFETY] REFUSED to push status/tag for {} "
                "lc_id={} because it lives in the banned campaign "
                "{!r}. This lead should be moved manually.",
                lead.phone, lc_id, campaign or BANNED_LEAKY_CAMPAIGN_NAME,
            )
            return False

        if not lc_id or not lc_id.isdigit():
            logger.warning(
                "[LeadMe] no numeric id in row for {} (row[1]={!r})",
                lead.phone, row[1] if len(row) > 1 else None,
            )
            return False

        status_val = _status_id_for_level(level)
        ok_status = True
        if status_val:
            ok_status = _admin_change_status(client, lc_id, status_val)

        ok_tag = True
        if slot:
            tag_lead_id = _resolve_tag_lead_id(client, lc_id)
            if tag_lead_id is None:
                # Resolution failed (viewLead probably returned a login
                # page). Pushing the tag against lc_id silently
                # succeeds but never lands -- so queue instead of
                # firing into the void.
                logger.warning(
                    "[LeadMe] slot tag pending: could not resolve internal "
                    "leadId for phone={} lc_id={} slot={!r}; queueing for "
                    "later retry", lead.phone, lc_id, slot,
                )
                leadme_queue.enqueue_engagement(
                    lead, level=level, slot=slot, note=note,
                )
                ok_tag = False
            else:
                ok_tag = _admin_add_tag(
                    client, tag_lead_id, f"חלון · {slot}",
                )

        logger.info(
            "[LeadMe admin] pushed lead {} lc_id={} campaign={!r} "
            "level={} status={} slot={!r} (status_ok={}, tag_ok={})",
            lead.phone, lc_id, campaign, level, status_val or "-",
            slot, ok_status, ok_tag,
        )
        return ok_status and ok_tag
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _admin_change_status(client, leadme_id: str, status_id: str) -> bool:
    """POST /app/leads/changeLeadsStatus. Returns True on ``result:true``."""
    if not (status_id or "").strip():
        return True
    base = get_settings().leadme_admin_base
    csrf = client.cookies.get("csrf_cookie_name") \
        or client.__dict__.get("_csrf_token") or ""
    payload = {
        "data[status]": str(status_id),
        "data[leadId]":  str(leadme_id),
        "csrf_lmcms":    csrf,
    }
    try:
        resp = client.post(base + "/app/leads/changeLeadsStatus", data=payload)
    except httpx.HTTPError as e:
        logger.error("[LeadMe admin status] HTTP error: {}", e)
        return False
    if resp.status_code != 200:
        logger.warning(
            "[LeadMe admin status] HTTP {} leadme_id={} status={} body={!r}",
            resp.status_code, leadme_id, status_id, resp.text[:200],
        )
        return False
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        # HTML instead of JSON almost always means the session cookies
        # expired and we got the login page back. Flag it clearly so
        # ops sees "refresh cookies" in the logs rather than a generic
        # parse error.
        preview = resp.text[:200]
        looks_like_login = "login" in preview.lower() or "recaptcha" in preview.lower()
        logger.warning(
            "[LeadMe admin status] non-JSON for leadme_id={} "
            "(likely_session_expired={}): {!r}",
            leadme_id, looks_like_login, preview,
        )
        return False
    if not body.get("result"):
        logger.warning(
            "[LeadMe admin status] rejected leadme_id={} status={}: {!r}",
            leadme_id, status_id, body,
        )
        return False
    logger.info(
        "[LeadMe admin status] leadme_id={} -> {}: {}",
        leadme_id, status_id, body.get("msg"),
    )
    return True


def _resolve_tag_lead_id(client, lc_id: str) -> Optional[str]:
    """Fetch viewLead page and extract the internal leadId for addLeadTag.

    LeadMe uses two different numeric IDs per lead:
    - ``lc_id`` (22xxxxxx): returned by getDataForTable, used for status
      changes and delete.
    - internal ``leadId`` (13xxxxxx): embedded in the ``viewLead``
      profile page HTML as ``uploadLeadProfileImage(<id>)``. Required
      by ``addLeadTag`` (posting the ``lc_id`` here silently returns
      ``result:true`` but the tag never lands).

    Returns the internal id on success, or ``None`` on any failure
    (page 404s, regex miss, session expired, network error). Callers
    MUST treat ``None`` as "don't push the tag yet" -- do NOT fall
    back to ``lc_id`` or the tag will vanish silently.
    """
    import re as _re2
    base = get_settings().leadme_admin_base
    try:
        resp = client.get(base + f"/app/leads/viewLead/{lc_id}")
    except httpx.HTTPError as e:
        logger.warning(
            "[LeadMe] viewLead HTTP error for lc_id={}: {}", lc_id, e,
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "[LeadMe] viewLead returned HTTP {} for lc_id={} -- "
            "cannot resolve internal leadId",
            resp.status_code, lc_id,
        )
        return None
    match = _re2.search(r"uploadLeadProfileImage\((\d+)\)", resp.text)
    if match:
        return match.group(1)
    preview = resp.text[:200]
    looks_like_login = "login" in preview.lower() or "recaptcha" in preview.lower()
    logger.warning(
        "[LeadMe] viewLead page for lc_id={} did not contain internal "
        "leadId (likely_session_expired={}); tag push will be deferred",
        lc_id, looks_like_login,
    )
    return None


def _admin_add_tag(client, leadme_id: str, tag: str) -> bool:
    """POST /app/ajax/addLeadTag. Returns True on ``result:true``.

    ``leadme_id`` must be the INTERNAL ``leadId`` (13xxxxxx range),
    not the ``lc_id`` (22xxxxxx range). Use :func:`_resolve_tag_lead_id`
    to convert.
    """
    if not (tag or "").strip():
        return True
    base = get_settings().leadme_admin_base
    csrf = client.cookies.get("csrf_cookie_name") \
        or client.__dict__.get("_csrf_token") or ""
    payload = {
        "text":       tag,
        "leadId":     str(leadme_id),
        "csrf_lmcms": csrf,
    }
    try:
        resp = client.post(base + "/app/ajax/addLeadTag", data=payload)
    except httpx.HTTPError as e:
        logger.error("[LeadMe admin tag] HTTP error: {}", e)
        return False
    if resp.status_code != 200:
        logger.warning(
            "[LeadMe admin tag] HTTP {} leadme_id={} tag={!r} body={!r}",
            resp.status_code, leadme_id, tag, resp.text[:200],
        )
        return False
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        preview = resp.text[:200]
        looks_like_login = "login" in preview.lower() or "recaptcha" in preview.lower()
        logger.warning(
            "[LeadMe admin tag] non-JSON leadme_id={} tag={!r} "
            "(likely_session_expired={}): {!r}",
            leadme_id, tag, looks_like_login, preview,
        )
        return False
    ok = bool(body.get("result"))
    logger.info("[LeadMe admin tag] leadme_id={} tag={!r} ok={} body={!r}",
                leadme_id, tag, ok, body)
    return ok


# --- session health -----------------------------------------------------

# Cache the last health check for a short window so the admin UI can
# render a pill on every page load without hammering LeadMe. Values:
# ("healthy" | "expired" | "unreachable" | "no_cookies", detail, ts).
_HEALTH_CACHE_TTL_SECONDS = 30
_HEALTH_CACHE: dict = {"result": None, "checked_at": 0.0}


def check_leadme_session_health(force: bool = False) -> dict:
    """Return a snapshot of whether the LeadMe admin session is alive.

    Result shape::

        {
            "status": "healthy" | "expired" | "unreachable" | "no_cookies",
            "detail": "<short human-readable note>",
            "checked_at": "<ISO timestamp UTC>",
            "cached": True|False,
        }

    Semantics:
    - ``healthy``      -> GET /app/leads returned 200 + a LeadMe-admin
                          HTML title marker. Everything downstream will
                          work.
    - ``expired``      -> got 200 but the body looks like the login /
                          reCAPTCHA page. Operator must refresh cookies
                          via /admin/leadme-cookies.
    - ``unreachable``  -> HTTP error, non-200, or network exception.
                          Could be transient (LeadMe outage) or DNS.
    - ``no_cookies``   -> LEADME_COOKIES_PATH doesn't point at a file,
                          the file is empty, or we can't build a client.

    Cached for ``_HEALTH_CACHE_TTL_SECONDS``. Pass ``force=True`` to
    bypass the cache (used by the admin "check now" button).
    """
    now_ts = time.time()
    cached = _HEALTH_CACHE.get("result")
    if (
        not force
        and cached is not None
        and now_ts - _HEALTH_CACHE.get("checked_at", 0.0) < _HEALTH_CACHE_TTL_SECONDS
    ):
        return {**cached, "cached": True}

    from datetime import datetime as _dt, timezone as _tz
    from app.crm.leadme_delete import _build_client

    checked_at_iso = _dt.now(_tz.utc).isoformat()

    client = _build_client()
    if client is None:
        result = {
            "status": "no_cookies",
            "detail": "לא נמצא קובץ עוגיות תקין. יש לרענן דרך /admin/leadme-cookies.",
            "checked_at": checked_at_iso,
        }
        _HEALTH_CACHE["result"] = result
        _HEALTH_CACHE["checked_at"] = now_ts
        return {**result, "cached": False}

    try:
        base = get_settings().leadme_admin_base
        try:
            resp = client.get(base + "/app/leads")
        except httpx.HTTPError as e:
            result = {
                "status": "unreachable",
                "detail": f"HTTP error: {e}",
                "checked_at": checked_at_iso,
            }
        else:
            if resp.status_code != 200:
                result = {
                    "status": "unreachable",
                    "detail": f"GET /app/leads returned HTTP {resp.status_code}",
                    "checked_at": checked_at_iso,
                }
            else:
                body_head = resp.text[:2000].lower()
                # LeadMe's authenticated admin pages carry a distinctive
                # marker in the <title> ("LeadMe CMS | ..."). The login
                # page also contains "leadme" text but reliably shows a
                # reCAPTCHA widget and a password input. We prefer a
                # positive check on the admin marker.
                admin_marker = "leadme cms |"
                login_markers = (
                    "recaptcha",
                    'name="password"',
                    "type=\"password\"",
                )
                if admin_marker in body_head:
                    result = {
                        "status": "healthy",
                        "detail": "GET /app/leads OK",
                        "checked_at": checked_at_iso,
                    }
                elif any(m in body_head for m in login_markers):
                    result = {
                        "status": "expired",
                        "detail": "התקבל דף התחברות במקום דף הלידים -- העוגיות פגו תוקף.",
                        "checked_at": checked_at_iso,
                    }
                else:
                    result = {
                        "status": "unreachable",
                        "detail": "לא זוהתה תשובה של LeadMe (לא דף לידים ולא דף לוגין).",
                        "checked_at": checked_at_iso,
                    }
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    _HEALTH_CACHE["result"] = result
    _HEALTH_CACHE["checked_at"] = now_ts
    if result["status"] != "healthy":
        logger.warning(
            "[LeadMe health] status={} detail={!r}",
            result["status"], result["detail"],
        )
    return {**result, "cached": False}


def push_status_via_admin(lead: Lead, status_id: str) -> bool:
    """Backwards-compat wrapper -- prefer :func:`push_lead`.

    Kept so any external caller referencing the old symbol still works.
    """
    from app.crm.leadme_delete import (
        _build_client, find_leadme_id_by_phone,
    )
    if not (status_id or "").strip():
        return True
    client = _build_client()
    if client is None:
        return False
    try:
        leadme_id = find_leadme_id_by_phone(lead.phone or "", client)
        if not leadme_id:
            return False
        return _admin_change_status(client, leadme_id, status_id)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def push_engagement_level(
    lead: Lead,
    level: int,
    note: Optional[str] = None,
    slot: Optional[str] = None,  # kept for API compat, slot is read from metadata
) -> bool:
    """Convenience wrapper: push an engagement level (1/2/3) to LeadMe.

    ``slot`` is accepted for API back-compat but IGNORED: the effective
    slot is always read from ``lead.lead_metadata['preferred_call_slot']``
    (that's the source of truth after ``schedule_call`` persists it).
    Pass slots via :func:`app.db.repository.update_lead_metadata` before
    calling this.

    Level semantics (numerically LOWER = more engaged):
        1 = booked a call.
        2 = replied to the bot.
        3 = never replied to the opener.

    Transitions we allow (engagement can only INCREASE over time):

        Any -> 1 (booked): always allowed. Book might happen after any
                           prior state, including cancel+rebook.
        3   -> 2 (silent lead replied): allowed. The bulk classifier
                           pushes Level 3 at scale, then a live reply
                           must upgrade to Level 2.
        None -> 2 / 3    : allowed (first-time classification).
        Same level        : no-op, idempotent.
        1 -> 2 / 3        : REFUSED (never downgrade a booked lead).
        2 -> 3            : REFUSED (a lead who replied isn't "silent").
    """
    if level not in (1, 2, 3):
        logger.warning("[LeadMe] ignoring invalid engagement level {}", level)
        return False
    # Deliberately accept the parameter and log if someone passed a
    # slot expecting it to be honored -- silent shadowing was a real
    # bug we hit before this cleanup.
    if slot:
        logger.debug(
            "[LeadMe] push_engagement_level(slot={!r}) argument is IGNORED "
            "-- slot must be persisted via update_lead_metadata first.",
            slot,
        )

    md = dict(lead.lead_metadata or {})
    already = md.get("leadme_last_level")
    already_int = int(already) if already is not None else None

    # Same level -> nothing to do.
    if already_int == level:
        logger.info(
            "[LeadMe] lead {} already at level {}, skipping duplicate",
            lead.phone, level,
        )
        return True

    # Booked never downgrades.
    if already_int == 1 and level in (2, 3):
        logger.info(
            "[LeadMe] lead {} is already booked (L1); refusing downgrade "
            "to L{}", lead.phone, level,
        )
        return True

    # Replied never downgrades to silent.
    if already_int == 2 and level == 3:
        logger.info(
            "[LeadMe] lead {} already replied (L2); refusing downgrade "
            "to L3", lead.phone,
        )
        return True

    # 3 -> 2, 3 -> 1, 2 -> 1, None -> any: proceed.
    ok = push_lead(lead, note=note, level=level)
    if ok:
        md["leadme_last_level"] = int(level)
        lead.lead_metadata = md
    return ok


def push_lead_cancellation(lead: Lead, reason: Optional[str] = None) -> bool:
    """Mark a previously-scheduled call as cancelled in LeadMe.

    Uses the admin-only path. Flips the status back to plain "חדש"
    (rel=1) so the sales team can re-book without confusion. No tag
    is pushed -- the customer asked us to keep the LeadMe UI clean
    (only ``חלון · <slot>`` tags are used now).
    """
    from app.crm.leadme_delete import (
        _build_client, find_leadme_id_by_phone,
    )

    settings = get_settings()

    if settings.leadme_test_mode:
        logger.info(
            "[LeadMe TEST_MODE] skipping cancel_lead for {} (test mode on)",
            lead.phone,
        )
        return True
    if _is_test_phone(lead.phone):
        logger.warning(
            "[LeadMe] REFUSING cancel for test-prefix phone {}", lead.phone,
        )
        return True

    if not (lead.phone or "").strip():
        return True

    client = _build_client()
    if client is None:
        logger.warning(
            "[LeadMe cancel] no admin cookies; cannot mark cancel for {}",
            lead.phone,
        )
        return False
    try:
        leadme_id = find_leadme_id_by_phone(lead.phone, client)
        if not leadme_id:
            logger.warning(
                "[LeadMe cancel] phone {} not found in LeadMe (no-op) "
                "reason={!r}", lead.phone, reason,
            )
            return True
        ok_status = _admin_change_status(client, leadme_id, "1")
        logger.info(
            "[LeadMe cancel] leadme_id={} phone={} reason={!r} status_ok={}",
            leadme_id, lead.phone, reason, ok_status,
        )
        return ok_status
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
