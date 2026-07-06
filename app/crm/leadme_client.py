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

    payload: Dict[str, str] = {
        "action": "new_lead",
        "fullname": display or lead.phone or "",
        "firstname": first,
        "lastname": last,
        "phone": lead.phone or "",
        "email": md.get("email") or "",
        "comment": " | ".join(note_parts),
        "tags": settings.leadme_source_label,
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


def push_lead(lead: Lead, note: Optional[str] = None) -> bool:
    """Push the lead into LeadMe.

    Creates or upserts via `LEADME_INSERT_URL` (dedupe on phone within the
    campaign). If `LEADME_UPDATE_URL` is also set and a status id is
    provided, follow up with a status update so the sales team sees the
    lead in the correct pipeline column.
    """
    settings = get_settings()
    insert_url = (settings.leadme_insert_url or "").strip()
    update_url = (settings.leadme_update_url or "").strip()
    status_val = (settings.leadme_status_id or "").strip()

    payload = _build_insert_payload(lead, note)

    if not insert_url:
        logger.info(
            "[LeadMe STUB] LEADME_INSERT_URL not set. Would POST payload={} for {}",
            payload, lead.phone,
        )
        return True

    ok, detail = _post(insert_url, payload)
    if not ok:
        logger.error("[LeadMe insert] failed for {}: {}", lead.phone, detail)
        return False
    logger.info("[LeadMe insert] pushed lead {} -> {}", lead.phone, detail)

    if update_url and status_val and lead.phone:
        upd_payload = {
            "phone": lead.phone,
            "status": status_val,
            "comment": payload.get("comment", ""),
        }
        ok2, detail2 = _post(update_url, upd_payload)
        if not ok2:
            logger.warning(
                "[LeadMe update] status update failed for {} (insert still ok): {}",
                lead.phone, detail2,
            )
        else:
            logger.info(
                "[LeadMe update] status={} for {} -> {}",
                status_val, lead.phone, detail2,
            )

    return True
