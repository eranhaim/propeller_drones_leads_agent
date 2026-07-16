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
    """Sync an engagement change to LeadMe.

    By default we call the UPDATE endpoint only. LeadMe leads originate
    from their own webhook (customer's website form -> LeadMe -> us), so
    a supplier INSERT creates a duplicate. Update-by-phone modifies the
    existing lead in whatever campaign it lives in.

    ``level`` picks the engagement status / tag (see ``LEVEL_TAGS``):
        1 = booked, 2 = replied but no booking, 3 = never replied.

    Set ``LEADME_INSERT_MODE=insert-then-update`` to fall back to the old
    "insert then update" behavior (needed if the lead genuinely did not
    come through LeadMe first).

    Two hard guards remain from before:
    - ``leadme_test_mode`` on -> full no-op (log only).
    - phone starting with the eval-harness ``999`` prefix -> full no-op.
    """
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
        logger.info("[LeadMe] insert_mode=never, skipping push for {}", lead.phone)
        return True

    insert_url = (settings.leadme_insert_url or "").strip()
    update_url = (settings.leadme_update_url or "").strip()
    status_val = _status_id_for_level(level)

    payload = _build_insert_payload(lead, note)

    # Ensure the engagement-level tag is applied even when the customer
    # hasn't configured status IDs yet. Tags is a comma-separated field.
    existing_tags = [
        t.strip() for t in (payload.get("tags") or "").split(",") if t.strip()
    ]
    level_tag = LEVEL_TAGS.get(level)
    if level_tag and level_tag not in existing_tags:
        existing_tags.append(level_tag)
    if existing_tags:
        payload["tags"] = ",".join(existing_tags)

    if mode == "insert-then-update":
        if not insert_url:
            logger.info(
                "[LeadMe STUB] LEADME_INSERT_URL not set. Would POST "
                "payload={} for {}", payload, lead.phone,
            )
        else:
            ok, detail = _post(insert_url, payload)
            if not ok:
                logger.error("[LeadMe insert] failed for {}: {}",
                             lead.phone, detail)
                return False
            logger.info("[LeadMe insert] pushed lead {} -> {}",
                        lead.phone, detail)
    else:
        logger.info(
            "[LeadMe] insert_mode={}, skipping insert for {} (avoiding "
            "duplicate creation)", mode, lead.phone,
        )

    if not update_url or not lead.phone:
        logger.info(
            "[LeadMe update STUB] no update_url or phone for lead {} "
            "(level={})", lead.phone, level,
        )
        return True

    upd_payload = {
        "phone": lead.phone,
        "comment": payload.get("comment", ""),
    }
    if status_val:
        upd_payload["status"] = status_val
    if payload.get("tags"):
        upd_payload["tags"] = payload["tags"]

    ok2, detail2 = _post(update_url, upd_payload)
    if not ok2:
        logger.warning(
            "[LeadMe update] status={} tags={!r} for {} FAILED: {}",
            status_val, payload.get("tags"), lead.phone, detail2,
        )
        return False
    logger.info(
        "[LeadMe update] level={} status={} tag={!r} for {} -> {}",
        level, status_val or "-", level_tag, lead.phone, detail2,
    )
    return True


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

    Used when the user says "actually no, cancel that call" after the bot
    already pushed them as ready_for_call. We do NOT delete the LeadMe lead
    (it still needs to be visible to sales) -- we just append a comment
    explaining the cancellation so a human can follow up cleanly. If
    ``LEADME_UPDATE_URL`` is set we also POST a status update carrying the
    "cancelled" note.
    """
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

    update_url = (settings.leadme_update_url or "").strip()
    if not update_url or not lead.phone:
        logger.info(
            "[LeadMe cancel STUB] no LEADME_UPDATE_URL or phone; would "
            "cancel lead {} (reason={!r})", lead.phone, reason,
        )
        return True

    comment = "SLOT CANCELLED BY LEAD"
    if reason:
        comment += f": {reason}"
    upd_payload = {
        "phone": lead.phone,
        "status": (settings.leadme_status_id or "").strip() or "1",
        "comment": comment,
    }
    ok, detail = _post(update_url, upd_payload)
    if ok:
        logger.info("[LeadMe cancel] pushed cancellation for {} -> {}",
                    lead.phone, detail)
    else:
        logger.error("[LeadMe cancel] failed for {}: {}", lead.phone, detail)
    return ok
