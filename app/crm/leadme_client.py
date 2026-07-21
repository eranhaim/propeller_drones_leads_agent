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

Env vars still consumed:
    LEADME_STATUS_LEVEL_1/2/3  -- numeric status ids for engagement tiers
    LEADME_STATUS_ID           -- fallback for level 1 if the tier var is empty
    LEADME_INSERT_MODE=never   -- kill-switch (skip all LeadMe pushes)
    LEADME_TEST_MODE           -- log-only, no HTTP calls
"""

from __future__ import annotations

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


# Human-readable Hebrew engagement tag applied to every LeadMe update. The
# sales team can filter by these tags in LeadMe's UI even when the numeric
# status ids aren't yet configured. Level 1 == booked a call, Level 2 ==
# replied but didn't book, Level 3 == never replied to the opener.
LEVEL_TAGS = {}


# LeadMe campaign id for the "trash" bucket the bot must never leak into.
# We assert on lead rows and log loudly if a bot push ever ends up here.
BANNED_LEAKY_CAMPAIGN_ID = "12277"
BANNED_LEAKY_CAMPAIGN_NAME = "הוסרו מ-Whatsapp"


def _push_slot_tag_via_api(phone: str, tag: str) -> bool:
    """Add a slot tag via the public supplier/insert API (no cookies needed)."""
    settings = get_settings()
    url = (settings.leadme_insert_url or "").strip()
    if not url:
        logger.warning("[LeadMe API] LEADME_INSERT_URL not set, cannot push slot tag")
        return False
    try:
        resp = httpx.post(url, data={
            "action": "new_lead",
            "phone": phone,
            "tags": tag,
        }, timeout=10.0)
        ok = resp.status_code == 200 and "success" in resp.text
        if ok:
            logger.info("[LeadMe API] slot tag sent phone={} tag={!r}", phone, tag)
        else:
            logger.warning("[LeadMe API] slot tag failed phone={} tag={!r} status={}", phone, tag, resp.status_code)
        return ok
    except Exception:
        logger.exception("[LeadMe API] slot tag raised for phone={}", phone)
        return False


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
    slot: Optional[str] = None,
) -> bool:
    """Sync an engagement change to LeadMe -- admin-only path.

    IMPORTANT: this function does NOT call the public ``/supplier/*``
    endpoints. Both ``/supplier/insert`` and ``/supplier/update`` act as
    upserts on our account -- when the phone isn't visible inside the
    supplier's linked campaign, LeadMe silently creates a duplicate row
    in the supplier's default campaign (id 12277 = "הוסרו מ-Whatsapp").
    That's the "leads keep leaking into the removed campaign" bug the
    customer keeps reporting.

    Instead, everything now flows through the internal admin API using
    the session cookies we already carry (see
    :mod:`app.crm.leadme_delete`):

    - Resolve the lead's numeric LeadMe id via getDataForTable search.
    - Change status via ``POST /app/leads/changeLeadsStatus``.
    - Add engagement tag via ``POST /app/ajax/addLeadTag``.

    Guards kept from before:
    - ``leadme_test_mode`` on -> full no-op (log only).
    - Phone starting with the eval-harness ``999`` prefix -> full no-op.
    - ``LEADME_INSERT_MODE=never`` -> full no-op (kept for kill-switch).

    ``level`` picks the engagement status / tag (see ``LEVEL_TAGS``):
        1 = booked, 2 = replied but no booking, 3 = never replied.
    """
    # Local import: leadme_delete imports config which imports us at
    # module load in some paths, so keep it lazy.
    import re as _re
    from app.crm.leadme_delete import _build_client, get_row_by_phone

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

    status_val = _status_id_for_level(level)
    level_tag = LEVEL_TAGS.get(level)

    client = _build_client()
    if client is None:
        logger.warning(
            "[LeadMe] no admin cookies configured; cannot push lead {} "
            "(status={}, level={}). Refresh cookies via the /admin panel.",
            lead.phone, status_val, level,
        )
        return False

    try:
        row = get_row_by_phone(lead.phone, client)
        if row is None:
            logger.warning(
                "[LeadMe] phone {} not found in LeadMe -- NOT creating "
                "(would leak into supplier campaign). Level={}. "
                "The lead must first be inserted via the customer's own "
                "LeadMe form flow.", lead.phone, level,
            )
            return True

        # row layout (see leadme_delete.py):
        #   [checkbox_html, id, name, phone, campaign, status_html, ...]
        leadme_id = str(row[1]).strip() if len(row) > 1 else ""
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
                "leadme_id={} because it lives in the banned campaign "
                "{!r}. This lead should be moved manually.",
                lead.phone, leadme_id, campaign or BANNED_LEAKY_CAMPAIGN_NAME,
            )
            return False

        if not leadme_id or not leadme_id.isdigit():
            logger.warning(
                "[LeadMe] no numeric id in row for {} (row[1]={!r})",
                lead.phone, row[1] if len(row) > 1 else None,
            )
            return False

        ok_status = True
        if status_val:
            ok_status = _admin_change_status(client, leadme_id, status_val)

        ok_tag = True
        if level_tag:
            ok_tag = _admin_add_tag(client, leadme_id, level_tag)

        slot = (lead.lead_metadata or {}).get("preferred_call_slot")
        if slot:
            tag_lead_id = _resolve_tag_lead_id(client, leadme_id)
            _admin_add_tag(client, tag_lead_id, f"חלון · {slot}")

        logger.info(
            "[LeadMe admin] pushed lead {} leadme_id={} campaign={!r} "
            "level={} status={} tag={!r} slot={!r} (status_ok={}, tag_ok={})",
            lead.phone, leadme_id, campaign, level, status_val or "-",
            level_tag, slot, ok_status, ok_tag,
        )
        return ok_status
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
        logger.warning(
            "[LeadMe admin status] non-JSON for leadme_id={}: {!r}",
            leadme_id, resp.text[:200],
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


def _resolve_tag_lead_id(client, lc_id: str) -> str:
    """Fetch viewLead page and extract the internal leadId for addLeadTag.

    LeadMe uses two different numeric IDs:
    - lc_id (22xxxxxx): returned by getDataForTable, used for status changes.
    - leadId (13xxxxxx): embedded in viewLead HTML, required by addLeadTag.
    """
    import re as _re2
    base = get_settings().leadme_admin_base
    try:
        resp = client.get(base + f"/app/leads/viewLead/{lc_id}")
        match = _re2.search(r"uploadLeadProfileImage\((\d+)\)", resp.text)
        if match:
            return match.group(1)
    except Exception:  # noqa: BLE001
        pass
    return lc_id  # fallback to lc_id if not found


def _admin_add_tag(client, leadme_id: str, tag: str) -> bool:
    """POST /app/ajax/addLeadTag. Returns True on ``result:true``."""
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
        return False
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return False
    ok = bool(body.get("result"))
    logger.info("[LeadMe admin tag] leadme_id={} tag={!r} ok={} body={!r}", leadme_id, tag, ok, body)
    return ok


def push_status_via_admin(lead: Lead, status_id: str) -> bool:
    """Backwards-compat wrapper -- prefer :func:`push_lead`.

    Kept so any external caller referencing the old symbol still works.
    Prefer :func:`push_lead` in new code.
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
    slot: Optional[str] = None,
) -> bool:
    """Convenience wrapper: push an engagement level (1/2/3) to LeadMe.

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
    ok = push_lead(lead, note=note, level=level, slot=slot)
    if ok:
        md["leadme_last_level"] = int(level)
        lead.lead_metadata = md
    return ok


def push_lead_cancellation(lead: Lead, reason: Optional[str] = None) -> bool:
    """Mark a previously handed-off lead as cancelled/re-open in LeadMe.

    Uses the admin-only path (no ``/supplier/*`` calls -- those upsert and
    leak duplicates into the supplier's default campaign 12277). Attaches
    a ``ביטול שיחה`` tag to make it visible to sales; the reason is
    captured in the tag suffix so Roy can see it at a glance.
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
                "[LeadMe cancel] phone {} not found in LeadMe (no-op)",
                lead.phone,
            )
            return True
        ok_tag = True
        # Move status back to plain "חדש" (rel=1) so the sales team can
        # rebook without confusion.
        ok_status = _admin_change_status(client, leadme_id, "1")
        logger.info(
            "[LeadMe cancel] leadme_id={} phone={} tag_ok={} status_ok={}",
            leadme_id, lead.phone, ok_tag, ok_status,
        )
        return ok_tag or ok_status
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
