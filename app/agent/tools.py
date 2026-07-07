"""LangChain tools the agent uses to think, remember, and act."""

from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool
from loguru import logger

from app.agent.classifier import apply_classification
from app.agent.context import current_context
from app.crm.client import mark_ready_for_call
from app.db import repository
from app.db.models import FunnelStage
from app.rag.retriever import search_as_text
from app.videos.catalog import get_video, recommend


@tool
def search_knowledge(query: str, topic: Optional[str] = None) -> str:
    """Search Propeller Drones' knowledge base (website + docs).

    Use this whenever you need a factual answer about the company, courses,
    prices, licenses, services, or industry regulations. Prefer very
    specific queries in Hebrew (e.g. "עלות רישיון עד 25 קג", "משך קורס
    תיאוריה", "מדריכים באקדמיה"). Pass ``topic`` to filter:

    - ``course_details`` -- flagship course-details page (salaries, instructors,
      industries, "95% first-try pass" stat, full FAQ). Prefer this for
      convincing/positioning conversations.
    - ``course_license`` -- commercial license focus (25kg vs 2000kg, CAAI exam).
    - ``courses`` -- general academy overview.
    - ``service_flight`` / ``service_washing`` -- commercial services offered.
    - ``shop`` -- e-commerce store (drone models, pricing policy, DJI/Enterprise).
    - ``faq`` -- FAQ from real leads (career, license, salary, licensing rules,
      buying vs. flying, aftermath of the course).
    - ``locations`` -- physical addresses of Propeller (Kokhav Yair HQ +
      Latrun flight training center). Use when asked "where is the course?".
    - ``about`` -- company overview / homepage.
    - ``general`` -- everything else.
    """
    logger.info("Tool search_knowledge: query='{}' topic={}", query, topic)
    return search_as_text(query, k=5, topic=topic)


@tool
def classify_lead(
    familiarity: Optional[str] = None,
    stage: Optional[str] = None,
    intent: Optional[str] = None,
    industry: Optional[str] = None,
    preferred_call_slot: Optional[str] = None,
    has_experience: Optional[bool] = None,
) -> str:
    """Update the lead's classification and captured facts.

    All fields are optional -- pass only what you have new info on.

    - ``familiarity``: ``beginner`` / ``aware`` / ``experienced``.
    - ``stage``: ``new`` / ``engaged`` / ``warm`` / ``ready_for_call`` /
      ``handed_off``.
    - ``intent``: what the lead actually wants:
        * ``course`` -- interested in the drone-flying course
        * ``shop`` -- wants to buy a drone / accessories
        * ``service`` -- wants Propeller to perform a commercial drone job
          (mapping, security, agriculture, washing...)
        * ``hobby`` -- personal / recreational use only, not commercial
        * ``unknown``
    - ``industry``: one of ``security``, ``solar``, ``agriculture``,
      ``mapping``, ``infrastructure``, ``cinema``, ``delivery``, ``washing``,
      ``other`` -- or a free-form Hebrew phrase.
    - ``preferred_call_slot``: ``9-12`` / ``12-15`` / ``15-18`` / ``any``
      / ``none``.
    - ``has_experience``: True/False -- do they have prior drone flying
      experience?

    Call this whenever new information changes your picture of the lead.
    """
    ctx = current_context()
    logger.info(
        "Tool classify_lead lead={} familiarity={} stage={} intent={} "
        "industry={} slot={} experience={}",
        ctx.lead.id, familiarity, stage, intent, industry,
        preferred_call_slot, has_experience,
    )
    apply_classification(ctx.session, ctx.lead, familiarity=familiarity, stage=stage)
    repository.update_lead_metadata(
        ctx.session,
        ctx.lead,
        intent=intent,
        industry=industry,
        preferred_call_slot=preferred_call_slot,
        has_experience=has_experience,
    )
    md = ctx.lead.lead_metadata or {}
    return (
        f"עודכן. רמת היכרות={ctx.lead.familiarity_level.value}, "
        f"שלב={ctx.lead.funnel_stage.value}, "
        f"כוונה={md.get('intent', '-')}, "
        f"תעשייה={md.get('industry', '-')}, "
        f"שעת התקשרות={md.get('preferred_call_slot', '-')}."
    )


@tool
def send_video(video_id: str, caption: Optional[str] = None) -> str:
    """Send a video to the lead via WhatsApp.

    Pass the ``video_id`` from the catalog shown in the system prompt.
    ``caption`` is optional text that appears with the video (keep it very
    short -- one sentence). Do not send the same video twice.
    """
    ctx = current_context()
    video = get_video(video_id)
    if video is None:
        return f"שגיאה: אין סרטון עם id={video_id} בקטלוג."

    if video.id in (ctx.lead.videos_sent or []):
        return f"הסרטון {video_id} כבר נשלח ללקוח -- לא נשלח שוב."

    if ctx.send_video is None:
        logger.warning("send_video called but no sender configured")
        return "שגיאה טכנית: לא ניתן לשלוח סרטונים כרגע."

    try:
        ctx.send_video(video, caption)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send video")
        return f"שגיאת שליחה: {exc}"

    repository.mark_video_sent(ctx.session, ctx.lead, video.id)
    return f"הסרטון '{video.title}' נשלח בהצלחה."


@tool
def recommend_video(topics_context: Optional[str] = None) -> str:
    """Ask the system to suggest which video is best for the current lead.

    Returns a video id you can then pass to ``send_video``. Optionally pass
    ``topics_context`` (a short Hebrew phrase describing what the lead just
    talked about) to bias the pick.
    """
    ctx = current_context()
    ctx_list = [topics_context] if topics_context else []
    v = recommend(
        familiarity=ctx.lead.familiarity_level.value,
        topics_context=ctx_list,
        exclude_ids=ctx.lead.videos_sent or [],
    )
    if v is None:
        return "אין סרטון מומלץ כרגע (או שכולם כבר נשלחו)."
    return (
        f"מומלץ: {v.id} - {v.title}. "
        f"(תיאור: {v.description})"
    )


@tool
def schedule_call(summary: Optional[str] = None) -> str:
    """Mark the lead as ready for a sales call.

    Updates the funnel stage to ``handed_off`` and pushes the status to
    the CRM (currently a stub -- see ``app/crm/client.py``). Only call
    this AFTER you have (a) an explicit yes from the lead, and (b) captured
    their ``preferred_call_slot`` via ``classify_lead``. ``summary`` is an
    optional short internal note about the lead (in Hebrew).

    After calling this, thank the lead in Hebrew and tell them the rep will
    reach out in their preferred time window.
    """
    ctx = current_context()
    md = ctx.lead.lead_metadata or {}
    slot = md.get("preferred_call_slot")
    logger.info(
        "Tool schedule_call lead={} phone={} slot={} summary={!r}",
        ctx.lead.id, ctx.lead.phone, slot, summary,
    )

    if not slot:
        return (
            "לא הועבר עדיין -- חסר preferred_call_slot. "
            "שאל את הלקוח באיזה חלון שעות עדיף לו (9-12 / 12-15 / 15-18), "
            "עדכן דרך classify_lead, ואז קרא ל-schedule_call שוב."
        )

    repository.update_funnel_stage(ctx.session, ctx.lead, FunnelStage.handed_off)

    md = ctx.lead.lead_metadata or {}
    note_parts = [
        f"intent={md.get('intent', '?')}",
        f"familiarity={ctx.lead.familiarity_level.value}",
        f"industry={md.get('industry', '?')}",
        f"slot={slot}",
        f"experience={md.get('has_experience', '?')}",
    ]
    if summary:
        note_parts.append(f"summary={summary}")

    # Wrap the CRM push -- we do NOT want to break the user-facing handoff
    # message if LeadMe is momentarily down, but we DO want the failure to
    # be loud in the logs so we can retry manually.
    try:
        ok = mark_ready_for_call(ctx.lead, note=" | ".join(note_parts))
        if ok:
            logger.info("schedule_call: LeadMe push succeeded for lead {}",
                        ctx.lead.id)
        else:
            logger.error("schedule_call: LeadMe push returned False for "
                         "lead {} (see leadme_client logs above)", ctx.lead.id)
    except Exception:
        logger.exception(
            "schedule_call: LeadMe push RAISED for lead {} -- lead is "
            "marked handed_off in our DB but did NOT reach LeadMe. "
            "Investigate & replay via app.crm.client.mark_ready_for_call",
            ctx.lead.id,
        )

    return (
        f"סומן להעברה למכירות והועבר ל-CRM (חלון: {slot}). "
        "כעת אמור ללקוח שנציג יצור איתו קשר בחלון הזה, ותודה לו."
    )


ALL_TOOLS = [
    search_knowledge,
    classify_lead,
    send_video,
    recommend_video,
    schedule_call,
]
