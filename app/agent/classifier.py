"""Lead familiarity + funnel-stage helpers.

The actual classification decision is made by the LLM (via the
``classify_lead`` tool) -- this module only validates and persists the
result, and describes each level in Hebrew for prompt injection.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.db import repository
from app.db.models import FamiliarityLevel, FunnelStage, Lead


FAMILIARITY_DESCRIPTIONS = {
    FamiliarityLevel.unknown: (
        "לא ידוע - עדיין לא נאסף מספיק מידע על הלקוח."
    ),
    FamiliarityLevel.beginner: (
        "מתחיל - לא מכיר את התחום המסחרי של רחפנים בכלל. "
        "צריך להסביר מה זה, איזה קריירה זו, ולמה כדאי."
    ),
    FamiliarityLevel.aware: (
        "מודע - יודע שיש תחום מסחרי לרחפנים, שוקל להיכנס. "
        "צריך להבליט את היתרונות של פרופלור על פני מתחרים."
    ),
    FamiliarityLevel.experienced: (
        "מנוסה - כבר טס רחפנים או שיש לו רישיון. "
        "צריך לדבר על התמחויות מתקדמות והזדמנויות עבודה."
    ),
}

FUNNEL_DESCRIPTIONS = {
    FunnelStage.new: "חדש - הודעה ראשונה, לא היה שיח משמעותי עדיין.",
    FunnelStage.engaged: "מעורב - יש שיח פעיל, שואל שאלות ומגלה עניין.",
    FunnelStage.warm: "חם - מגלה עניין גבוה, שאל על מחירים/תאריכים/מסלולים.",
    FunnelStage.ready_for_call: "בשל לשיחה - מעוניין לדבר עם איש מכירות.",
    FunnelStage.handed_off: "הועבר - כבר קיבל קישור לתיאום ואיש מכירות ייצור קשר.",
}


def parse_familiarity(raw: str) -> Optional[FamiliarityLevel]:
    raw = (raw or "").strip().lower()
    try:
        return FamiliarityLevel(raw)
    except ValueError:
        logger.warning("Unknown familiarity value: {}", raw)
        return None


def parse_stage(raw: str) -> Optional[FunnelStage]:
    raw = (raw or "").strip().lower()
    try:
        return FunnelStage(raw)
    except ValueError:
        logger.warning("Unknown funnel stage value: {}", raw)
        return None


def apply_classification(
    session: Session,
    lead: Lead,
    familiarity: Optional[str] = None,
    stage: Optional[str] = None,
) -> Lead:
    """Apply an LLM-issued classification to the DB. Silently ignores bad values."""
    if familiarity:
        level = parse_familiarity(familiarity)
        if level is not None:
            repository.update_familiarity(session, lead, level)

    if stage:
        st = parse_stage(stage)
        if st is not None:
            repository.update_funnel_stage(session, lead, st)

    return lead


def describe_state(lead: Lead) -> str:
    """Multi-line description of the lead's current state for prompt injection."""
    fam = FAMILIARITY_DESCRIPTIONS.get(lead.familiarity_level, "")
    stg = FUNNEL_DESCRIPTIONS.get(lead.funnel_stage, "")
    videos = ", ".join(lead.videos_sent) if lead.videos_sent else "אף אחד"

    md = lead.lead_metadata or {}
    intent = md.get("intent", "לא ידוע עדיין")
    industry = md.get("industry", "לא ידוע עדיין")
    slot = md.get("preferred_call_slot", "לא נלכד עדיין")
    experience = md.get("has_experience")
    experience_str = (
        "כן" if experience is True
        else "לא" if experience is False
        else "לא ידוע עדיין"
    )

    return (
        f"מצב נוכחי של הליד:\n"
        f"- שם: {lead.name or 'לא ידוע'}\n"
        f"- רמת היכרות: {lead.familiarity_level.value} ({fam})\n"
        f"- שלב במשפך: {lead.funnel_stage.value} ({stg})\n"
        f"- כוונה (intent): {intent}\n"
        f"- תעשייה/תחום עניין: {industry}\n"
        f"- ניסיון קודם עם רחפנים: {experience_str}\n"
        f"- שעת התקשרות מועדפת: {slot}\n"
        f"- סרטונים שכבר נשלחו: {videos}"
    )
