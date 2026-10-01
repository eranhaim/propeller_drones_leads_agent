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
from sqlalchemy.orm import Session

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
    *,
    session: Optional[Session] = None,
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
        1 = configured priority source or booked call, 2 = meaningful reply,
        3 = no meaningful reply.

    This function writes the status only. The ``חלון · <slot>`` tag is owned
    by :func:`push_call_window_tag`; on a queued fallback the slot is carried
    on the pending item so the queue re-adds the tag when it drains.
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
        return False

    mode = (settings.leadme_insert_mode or "update-only").strip().lower()
    if mode == "never":
        logger.info("[LeadMe] insert_mode=never, skipping push for {}",
                    lead.phone)
        return False

    if not (lead.phone or "").strip():
        logger.info(
            "[LeadMe] skipping push for lead {} -- no phone number", lead.id,
        )
        return False

    slot = (lead.lead_metadata or {}).get("preferred_call_slot")

    client = _build_client()
    if client is None:
        logger.warning(
            "[LeadMe] no admin cookies configured; queueing push for lead "
            "{} (level={}, slot={}). Refresh cookies via the /admin panel.",
            lead.phone, level, slot,
        )
        leadme_queue.enqueue_engagement(
            lead, level=level, slot=slot, note=note, session=session,
        )
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
            leadme_queue.enqueue_engagement(
                lead, level=level, slot=slot, note=note, session=session,
            )
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
        if not status_val:
            logger.error(
                "[LeadMe] no configured status ID for level {}; refusing to "
                "record a local delivery for phone={}",
                level, lead.phone,
            )
            return False
        ok_status = _admin_change_status(client, lc_id, status_val)

        logger.info(
            "[LeadMe admin] pushed lead {} lc_id={} campaign={!r} "
            "level={} status={} slot={!r} (status_ok={})",
            lead.phone, lc_id, campaign, level, status_val or "-",
            slot, ok_status,
        )
        if ok_status:
            return True

        # A resolved row can still reject a write when cookies expire or
        # LeadMe returns a transient error. Preserve the engagement intent
        # (and slot, so the queue re-adds the window tag) for the durable
        # queue instead of falsely recording it locally.
        leadme_queue.enqueue_engagement(
            lead, level=level, slot=slot, note=note, session=session,
        )
        return True
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
    *,
    session: Optional[Session] = None,
) -> bool:
    """Convenience wrapper: push an engagement level (1/2/3) to LeadMe.

    The call-window tag is NOT handled here -- it is a separate concern owned
    by :func:`push_call_window_tag`. This keeps the tag independent of the
    level so a booked lead still gets tagged even when the status push is a
    same-level no-op.

    Level semantics (numerically LOWER = more engaged):
        1 = configured priority source or bot-confirmed booking.
        2 = meaningful WhatsApp reply without an L1 criterion.
        3 = no meaningful WhatsApp reply.

    Transitions we allow (engagement can only INCREASE over time):

        Any -> 1 : always allowed.
        3   -> 2 (silent lead replied): allowed. The bulk classifier
                           pushes Level 3 at scale, then a live reply
                           must upgrade to Level 2.
        None -> 2 / 3    : allowed (first-time classification).
        Same level        : no-op, idempotent.
        1 -> 2 / 3        : REFUSED (never downgrade an L1 lead).
        2 -> 3            : REFUSED (a lead who replied isn't "silent").
    """
    if level not in (1, 2, 3):
        logger.warning("[LeadMe] ignoring invalid engagement level {}", level)
        return False

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
    # push_lead and cancel_lead honour test mode; this path did not, so the
    # eval harness still called the v3 API with its synthetic 999... phones.
    # Placed after the upgrade-only guards and still recording the level
    # locally, so the harness observes exactly what production would record.
    if get_settings().leadme_test_mode:
        logger.info(
            "[LeadMe TEST_MODE] recording level {} for {} without calling LeadMe",
            level, lead.phone,
        )
        md["leadme_last_level"] = int(level)
        lead.lead_metadata = md
        return True

    # Try v3 API first (clean, no cookies); fall back to legacy cookie path.
    from app.crm.leadme_v3 import (
        get_lead_status,
        is_v3_available,
        level_for_status_id,
        push_level as _v3_push_level,
    )
    from app.crm import leadme_queue
    if is_v3_available():
        slot = (lead.lead_metadata or {}).get("preferred_call_slot")
        current_status = get_lead_status(phone=lead.phone or "")
        current_level = (
            level_for_status_id(current_status["status"])
            if current_status is not None
            else None
        )
        if current_level is not None and current_level < level:
            logger.info(
                "[LeadMe] refusing L{} for {} because LeadMe is already L{}",
                level,
                lead.phone,
                current_level,
            )
            leadme_queue.record_confirmed_engagement(lead, current_level)
        elif _v3_push_level(lead.phone, level=level):
            # Push actually landed in LeadMe -- record it and discard any
            # stale queued L2/L3 item. A queued L1 is retained.
            leadme_queue.record_confirmed_engagement(lead, level)
        else:
            # v3 failed (most likely CTWA race: lead not in LeadMe yet).
            # Enqueue for background retry. Do NOT write leadme_last_level
            # here -- the queue drain writes it when the push actually
            # lands. Writing it prematurely causes the downgrade guard to
            # block future pushes for a level that never reached LeadMe.
            logger.info(
                "[LeadMe] v3 push failed for {} level={} -- queueing for retry",
                lead.phone, level,
            )
            leadme_queue.enqueue_engagement(
                lead, level=level, slot=slot, note=note, session=session,
            )
    else:
        ok = push_lead(lead, note=note, level=level, session=session)
        pending = list((lead.lead_metadata or {}).get("leadme_push_pending") or [])
        has_pending_engagement = any(
            item.get("kind") == "engagement" for item in pending
        )
        if ok and not has_pending_engagement:
            # ``push_lead`` succeeded synchronously. If it queued instead,
            # the queue drain writes this key only after LeadMe confirms.
            leadme_queue.record_confirmed_engagement(lead, level)

    return True  # queued or pushed -- caller should not retry


def push_call_window_tag(
    lead: Lead,
    slot: Optional[str],
    *,
    session: Optional[Session] = None,
) -> bool:
    """Attach the ``חלון · <slot>`` tag to a booked lead via the v3 API.

    Sole owner of the direct (immediate) call-window tag. Independent of the
    engagement level so a booked L2 lead is tagged even when its status push
    is a same-level no-op. ``add_lead_tag`` is idempotent (it verifies the tag
    landed), so re-tagging is safe.

    Logs and continues on failure: a booked lead is already in LeadMe, so a
    missed tag is cosmetic, never a lost lead. When the lead is not yet visible
    in LeadMe (CTWA race), the queued engagement item carries the slot and the
    queue re-adds the tag on drain. ``session`` is unused (v3 is sessionless)
    and kept only for signature parity with the caller-facing ``mark_*`` API.
    """
    from app.crm.leadme_v3 import add_lead_tag, get_lead_id, is_v3_available

    settings = get_settings()
    slot = (slot or "").strip()
    if not slot or slot in ("any", "none"):
        return False
    if not (lead.phone or "").strip():
        return False
    if settings.leadme_test_mode or _is_test_phone(lead.phone):
        logger.info(
            "[LeadMe TEST_MODE] skipping call-window tag for {} (slot={})",
            lead.phone, slot,
        )
        return True
    if not is_v3_available():
        logger.warning(
            "[LeadMe] v3 unavailable; call-window tag for {} deferred to queue",
            lead.phone,
        )
        return False

    lead_id = get_lead_id(lead.phone)
    if lead_id is None:
        logger.warning(
            "[LeadMe] lead {} not visible in LeadMe yet; call-window tag "
            "deferred to queue (slot={})", lead.phone, slot,
        )
        return False

    tag = f"חלון · {slot}"
    ok = add_lead_tag(lead_id, tag)
    if not ok:
        logger.warning(
            "[LeadMe] call-window tag failed for {} tag={!r}", lead.phone, tag,
        )
    return ok


def push_not_relevant(
    lead: Lead,
    note: Optional[str] = None,
    *,
    session: Optional[Session] = None,
) -> bool:
    """Durably queue the configured terminal ``not relevant`` CRM status.

    This path intentionally does no synchronous LeadMe lookup: it is called
    from an inbound WhatsApp turn and must never delay the reply. The retry
    queue handles unavailable cookies, LeadMe's supplier-sync race, and
    restarts. A fresh inbound message clears the local relevance flag and
    resumes normal engagement classification.
    """
    settings = get_settings()
    status_id = (settings.leadme_status_not_relevant or "").strip()
    if not status_id.isdigit():
        logger.error(
            "[LeadMe] cannot mark lead {} not relevant: "
            "LEADME_STATUS_NOT_RELEVANT is not a numeric configured status",
            lead.phone,
        )
        return False
    if settings.leadme_test_mode or _is_test_phone(lead.phone):
        logger.info(
            "[LeadMe TEST_MODE] recording not-relevant state for {}",
            lead.phone,
        )
        return True
    if (settings.leadme_insert_mode or "").strip().lower() == "never":
        logger.info("[LeadMe] insert_mode=never, skipping not-relevant for {}", lead.phone)
        return True

    from app.crm import leadme_queue

    logger.info(
        "[LeadMe] queueing not-relevant status for {} status={} reason={!r}",
        lead.phone, status_id, note,
    )
    leadme_queue.enqueue_status(lead, status_id, session=session)
    return True


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
        if ok_status:
            # Reset leadme_last_level so push_engagement_level's
            # downgrade guard won't block the next classification.
            md = dict(lead.lead_metadata or {})
            md.pop("leadme_last_level", None)
            lead.lead_metadata = md
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
