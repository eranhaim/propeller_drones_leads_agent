"""LeadMe CMS client -- public "supplier" API.

Reverse-engineered from LeadMe's admin panel. The integration is meant to
be used exactly the same way LeadMe integrates Facebook, TikTok, and other
paid lead sources:

    1. In LeadMe: Preferences -> Suppliers -> New Supplier.
    2. Give it a name (e.g. "WhatsApp Bot") and set it Active.
    3. Check the campaign(s) this supplier is allowed to push into
       (e.g. campaign 12277 = "leads from WhatsApp").
    4. Save. LeadMe generates a public URL of the form
           https://api.leadmecms.co.il/supplier/insert/{link_id}/{slug}
       (visible in the supplier's "API" dialog on the same edit page).
    5. Also visible: an UPDATE URL of the form
           https://api.leadmecms.co.il/supplier/update/p/{slug}
       which accepts (phone, status, ...) to update an existing lead.

Both endpoints accept POST or GET, form-encoded or JSON, no auth headers.
They dedupe leads by phone within the campaign.

Env vars consumed (see .env.example):
    LEADME_INSERT_URL           full URL for POST /supplier/insert/...
                                (per supplier+campaign; empty => no-op stub)
    LEADME_UPDATE_URL           full URL for POST /supplier/update/p/{slug}
                                (empty => skip status update after insert)
    LEADME_STATUS_ID            status id or name to send with update
                                (e.g. "1", "new", "ready_for_call")
    LEADME_SOURCE_LABEL         value sent in `tags`

Custom lead field ids (`clf_XXXXX`) are Propeller-specific and mapped
against the questions currently on their public lead forms. If the account
changes its custom fields, update ``PROPELLER_CLF`` below.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.config import get_settings
from app.db.models import Lead

# --- Propeller-specific custom-field ids in LeadMe --------------------------
# Discovered by inspecting the "API" instructions dialog on the supplier form
# for this account. Values are exactly what the LeadMe admin shows.
PROPELLER_CLF = {
    "intent":              "clf_116982",  # "אני פה בשביל..."
    "residence_area":      "clf_116984",  # "מה איזור המגורים שלך?"
    "experience_level":    "clf_116981",  # "איזה ניסיון יש לך עם רחפנים?"
    "license_type":        "clf_117019",  # "איזה רישיון הטסת רחפנים יש לך?"
    "familiarity_1_to_5":  "clf_116142",  # "מ-1 עד 5, כמה אתם מכירים..."
    "course_of_interest":  "clf_116141",  # "באיזה קורס אתם מעוניינים?"
    "fields_of_interest":  "clf_116140",  # "איזה מהתחומים הבאים מעניין"
    "age_bucket":          "clf_116983",  # "מה הגיל שלך?"
    "has_drone_background": "clf_113565", # "יש רקע בהטסת רחפנים"
    "wants":               "clf_115314",  # "אני רוצה"
    "prior_experience":    "clf_115313",  # "האם יש לך ניסיון קודם"
    "interested_in":       "clf_115312",  # "אני מעוניין/ת"
}


def _split_name(display: str) -> tuple[str, str]:
    display = (display or "").strip()
    if not display:
        return "", ""
    if " " in display:
        first, last = display.split(" ", 1)
        return first.strip(), last.strip()
    return display, ""


def _build_insert_payload(lead: Lead, note: Optional[str]) -> Dict[str, str]:
    """Build the form-encoded payload for /supplier/insert/{link_id}/{slug}."""
    settings = get_settings()
    md: Dict[str, Any] = dict(lead.lead_metadata or {})

    display = (lead.name or "").strip()
    first, last = _split_name(display)

    note_parts: list[str] = []
    if note:
        note_parts.append(note)
    if lead.familiarity_level:
        note_parts.append(f"רמת היכרות: {lead.familiarity_level.value}")
    if lead.funnel_stage:
        note_parts.append(f"שלב משפך: {lead.funnel_stage.value}")
    if md.get("intent"):
        note_parts.append(f"מטרה: {md['intent']}")
    if md.get("industry"):
        note_parts.append(f"תחום: {md['industry']}")
    if md.get("preferred_call_slot"):
        note_parts.append(f"חלון שיחה מועדף: {md['preferred_call_slot']}")
    if md.get("has_experience") is not None:
        note_parts.append(f"ניסיון קיים: {md['has_experience']}")
    if lead.videos_sent:
        note_parts.append(f"סרטונים שנשלחו: {', '.join(lead.videos_sent)}")

    tag_parts: list[str] = []
    if settings.leadme_source_label:
        tag_parts.append(settings.leadme_source_label)
    slot = (md.get("preferred_call_slot") or "").strip()
    if slot and slot.lower() != "none":
        tag_parts.append(f"חלון: {slot}")

    payload: Dict[str, str] = {
        "action": "new_lead",
        "fullname": display or lead.phone or "",
        "firstname": first,
        "lastname": last,
        "phone": lead.phone or "",
        "email": md.get("email") or "",
        "comment": " | ".join(note_parts),
        "tags": ",".join(tag_parts),
        "businesscategory": md.get("industry") or "",
        "company": md.get("company") or "",
    }

    # Custom Propeller fields -- push whatever we've collected.
    clf_map = {
        "intent":              md.get("intent"),
        "residence_area":      md.get("residence_area"),
        "experience_level":    md.get("experience_level")
                               or ("יש" if md.get("has_experience") else
                                   "אין" if md.get("has_experience") is False
                                   else None),
        "license_type":        md.get("license_type"),
        "familiarity_1_to_5":  md.get("familiarity_1_to_5")
                               or (lead.familiarity_level.value
                                   if lead.familiarity_level else None),
        "course_of_interest":  md.get("course_of_interest"),
        "fields_of_interest":  md.get("fields_of_interest"),
        "age_bucket":          md.get("age_bucket"),
        "has_drone_background": md.get("has_experience"),
        "wants":               md.get("wants"),
        "prior_experience":    md.get("prior_experience")
                               or md.get("has_experience"),
        "interested_in":       md.get("interested_in") or md.get("intent"),
    }
    for logical, clf_id in PROPELLER_CLF.items():
        val = clf_map.get(logical)
        if val is None or val == "":
            continue
        payload[clf_id] = str(val)

    # Drop empties so we don't overwrite existing LeadMe data with blanks.
    return {k: v for k, v in payload.items() if v not in ("", None)}


def _post(url: str, data: Dict[str, str]) -> tuple[bool, str]:
    try:
        resp = httpx.post(url, data=data, timeout=15.0, follow_redirects=False)
    except httpx.HTTPError as e:
        return False, f"httpx error: {e}"
    body = (resp.text or "").strip()
    ok = resp.status_code == 200 and ("success" in body.lower() or body == "")
    # LeadMe's public API always returns 200. Content differentiates:
    #   {"type":"success","data":""}  -> lead created / accepted
    #   {"type":"error","data":""}    -> rejected (usually campaign not linked)
    #   ""                            -> update endpoint success
    return ok, f"{resp.status_code} {body[:200]}"


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
LEVEL_TAGS = {
    1: "רמה 1 · קבע שיחה",
    2: "רמה 2 · הגיב ולא קבע",
    3: "רמה 3 · לא הגיב",
}


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
    from app.crm.leadme_delete import (
        _build_client, find_leadme_id_by_phone,
    )

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
        leadme_id = find_leadme_id_by_phone(lead.phone, client)
        if not leadme_id:
            logger.warning(
                "[LeadMe] phone {} not found in LeadMe -- NOT creating "
                "(would leak into supplier campaign). Level={}. "
                "The lead must first be inserted via the customer's own "
                "LeadMe form flow.", lead.phone, level,
            )
            # This is the whole point of the refactor: we NEVER create a
            # new LeadMe row from the bot side. Return True so callers
            # don't retry aggressively; log warning so operators notice.
            return True

        ok_status = True
        if status_val:
            ok_status = _admin_change_status(client, leadme_id, status_val)
            if not ok_status:
                logger.warning(
                    "[LeadMe admin] status change failed for {} "
                    "leadme_id={} status={}",
                    lead.phone, leadme_id, status_val,
                )

        ok_tag = True
        if level_tag:
            ok_tag = _admin_add_tag(client, leadme_id, level_tag)

        logger.info(
            "[LeadMe admin] pushed lead {} leadme_id={} level={} "
            "status={} tag={!r} (status_ok={}, tag_ok={})",
            lead.phone, leadme_id, level, status_val or "-", level_tag,
            ok_status, ok_tag,
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
    if not ok:
        logger.warning(
            "[LeadMe admin tag] rejected leadme_id={} tag={!r}: {!r}",
            leadme_id, tag, body,
        )
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
    ok = push_lead(lead, note=note, level=level)
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
        tag = "ביטול שיחה"
        if reason:
            tag += f" · {reason[:40]}"
        ok_tag = _admin_add_tag(client, leadme_id, tag)
        # Move status back to plain "חדש" (rel=1) so the sales team can
        # rebook without confusion. We deliberately don't set a "cancelled"
        # status because LeadMe doesn't have one; the tag is enough.
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
