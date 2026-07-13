"""LangChain tools the agent uses to think, remember, and act."""

from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool
from loguru import logger

from app.agent.classifier import apply_classification
from app.agent.context import current_context
from app.crm.client import cancel_ready_for_call, mark_ready_for_call
from app.db import repository
from app.db.models import FunnelStage
from app.rag.retriever import search_as_text
from app.videos.catalog import get_video, recommend


# Canonical set of call-window values LeadMe / our sales team recognise.
# Anything else is a hallucination (customer flagged: "הנדסת בניין" got
# recorded as slot="12-15"). classify_lead REJECTS non-canonical values.
_VALID_SLOTS = {"9-12", "12-15", "15-18", "any", "none"}


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
    - ``hr`` -- HR contact info for job-seekers (hr@propeller-drones.com).
      Use when the lead asks about working AT Propeller, not about a course.
    - ``about`` -- company overview / homepage.
    - ``general`` -- everything else.

    IMPORTANT for the 25kg license question (customer flagged this bug):
    ``search_knowledge(topic="course_license", query="רישיון עד 25 קג")``
    returns the authoritative answer: theory-only, online CAAI exam, NO
    practical part required. Practical is only for the heavy (25-2000kg)
    track.
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
        * ``job`` -- looking for employment AT Propeller. Route to HR
          email (hr@propeller-drones.com), do NOT push a course, and mark
          ``stage=handed_off``.
        * ``unknown``
    - ``industry``: one of ``security``, ``solar``, ``agriculture``,
      ``mapping``, ``infrastructure``, ``cinema``, ``delivery``, ``washing``,
      ``other`` -- or a free-form Hebrew phrase.
    - ``preferred_call_slot``: MUST be EXACTLY one of ``9-12`` / ``12-15``
      / ``15-18`` / ``any`` / ``none``. Anything else (city names, industry
      names, free-text times like "13:00" or "in the morning") is REJECTED
      and logged as a bug. Only pass a value here when the lead has
      literally answered with one of the 3 canonical windows.
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

    # Reject non-canonical slot values so free-text like "הנדסת בניין" or
    # "תל אביב" never gets recorded as a preferred_call_slot. The prompt
    # already teaches this rule; this is a hard safety net.
    slot_rejected: Optional[str] = None
    if preferred_call_slot is not None:
        normalized = str(preferred_call_slot).strip().lower()
        if normalized not in _VALID_SLOTS:
            logger.warning(
                "[classify_lead] REJECTED bogus slot={!r} for lead {} "
                "(not one of {})",
                preferred_call_slot, ctx.lead.id, sorted(_VALID_SLOTS),
            )
            slot_rejected = preferred_call_slot
            preferred_call_slot = None  # don't persist it

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
    summary = (
        f"עודכן. רמת היכרות={ctx.lead.familiarity_level.value}, "
        f"שלב={ctx.lead.funnel_stage.value}, "
        f"כוונה={md.get('intent', '-')}, "
        f"תעשייה={md.get('industry', '-')}, "
        f"שעת התקשרות={md.get('preferred_call_slot', '-')}."
    )
    if slot_rejected is not None:
        summary += (
            f" ⚠️ התעלמתי מהערך '{slot_rejected}' עבור preferred_call_slot - "
            "חלון שעות חייב להיות אחד מ: 9-12 / 12-15 / 15-18 / any. "
            "אל תקבע שיחה עד שהליד נותן חלון חוקי במפורש."
        )
    return summary


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

    # In-turn dedup: if the LLM tries to send the same video twice within
    # the same agent turn, the second call is rejected before touching
    # GreenAPI. This catches the "sent Oded's video twice" bug the customer
    # flagged, since the DB commit for videos_sent only happens after the
    # first send returns.
    if video.id in ctx.videos_sent_this_turn:
        logger.warning(
            "[send_video] IN-TURN DUPLICATE blocked: lead {} tried to send "
            "video {!r} twice in one reply", ctx.lead.id, video.id,
        )
        return (
            f"הסרטון {video_id} כבר נשלח בהודעה הנוכחית -- אל תשלח אותו שוב."
        )
    ctx.videos_sent_this_turn.add(video.id)

    if ctx.send_video is None:
        logger.warning("send_video called but no sender configured")
        return "שגיאה טכנית: לא ניתן לשלוח סרטונים כרגע."

    try:
        ctx.send_video(video, caption)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send video")
        return f"שגיאת שליחה: {exc}"

    repository.mark_video_sent(ctx.session, ctx.lead, video.id)

    # Track webinar-specific send time so the follow-up scheduler can send
    # a "did you watch?" nudge tailored to the webinar rather than the
    # generic silence nudge.
    if video.id == "course_webinar_full":
        from datetime import datetime, timezone as _tz
        repository.update_lead_metadata(
            ctx.session, ctx.lead,
            webinar_sent_at=datetime.now(_tz.utc).isoformat(),
        )
        logger.info("[send_video] webinar sent -> tracking for follow-up (lead {})",
                    ctx.lead.id)

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
def schedule_call(
    summary: Optional[str] = None,
    preferred_call_slot: Optional[str] = None,
) -> str:
    """Mark the lead as ready for a sales call.

    Updates the funnel stage to ``handed_off`` and pushes the status to
    the CRM. Only call this AFTER you have an explicit yes from the lead
    and know their preferred time window.

    - ``summary`` -- optional short Hebrew internal note about the lead.
    - ``preferred_call_slot`` -- MUST be one of ``9-12`` / ``12-15`` /
      ``15-18`` / ``any``. Pass it here if the lead just gave you a slot
      in the current message; the tool will persist it before pushing to
      CRM. If omitted, the tool falls back to whatever slot was previously
      captured via ``classify_lead``.

    After calling this, thank the lead in Hebrew and tell them the rep will
    reach out in their preferred time window.
    """
    ctx = current_context()

    # Accept a slot passed inline (the common case: lead literally just
    # replied "12-15"). This closes the "schedule_call was called without
    # classify_lead first" bug that made the bot say "technical error"
    # even though nothing was broken.
    if preferred_call_slot is not None:
        normalized = str(preferred_call_slot).strip().lower()
        if normalized in _VALID_SLOTS and normalized != "none":
            repository.update_lead_metadata(
                ctx.session, ctx.lead, preferred_call_slot=normalized,
            )
            logger.info(
                "[schedule_call] inline slot={!r} persisted for lead {}",
                normalized, ctx.lead.id,
            )
        else:
            logger.warning(
                "[schedule_call] IGNORED bogus inline slot={!r} for lead {}",
                preferred_call_slot, ctx.lead.id,
            )

    md = ctx.lead.lead_metadata or {}
    slot = md.get("preferred_call_slot")
    logger.info(
        "Tool schedule_call lead={} phone={} slot={} summary={!r}",
        ctx.lead.id, ctx.lead.phone, slot, summary,
    )

    if not slot:
        # This is NOT a technical error -- the tool executed fine, we just
        # don't have a slot yet. The wording is deliberately explicit to
        # stop the LLM from triggering the "yesh li beaya technit" rule.
        return (
            "NOT_AN_ERROR: אין עדיין חלון שעות מועדף. "
            "אל תגיד ללקוח שיש תקלה טכנית. פשוט שאל אותו: "
            "'באיזה חלון שעות עדיף לך שהיועץ יתקשר: 9-12, 12-15, או 15-18?'. "
            "כשהוא יענה, קרא ל-schedule_call(preferred_call_slot=\"<תשובתו>\")."
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
        "כעת אמור ללקוח שיועץ הלימודים יצור איתו קשר בחלון הזה, ותודה לו."
    )


@tool
def cancel_call(reason: Optional[str] = None) -> str:
    """Cancel a previously-scheduled call and reset the lead's booking state.

    Call this ONLY when the lead explicitly says the booking is wrong or
    they want to change/cancel it (e.g. "לא זה לא נכון", "תבטל", "תשנה
    את השעה", "רגע, זה טעות"). It:

    - Clears ``preferred_call_slot`` in ``lead_metadata``.
    - Sends a cancellation note to LeadMe so the sales rep sees it.
    - Rewinds ``funnel_stage`` from ``handed_off`` back to ``warm`` so the
      normal booking flow can happen again once the lead gives a valid
      slot.

    After calling this, apologize briefly, then either:

    - If the lead's message ALREADY contained a valid new slot
      (e.g. "רגע זה טעות, אני רוצה בבוקר בין 9 ל-12"), immediately
      call ``schedule_call(preferred_call_slot="9-12")`` in the same
      turn -- do NOT make them repeat themselves.
    - Otherwise, ask them for the correct window (9-12 / 12-15 /
      15-18) and wait for their reply before scheduling.
    """
    ctx = current_context()
    md = ctx.lead.lead_metadata or {}
    old_slot = md.get("preferred_call_slot")

    logger.info(
        "Tool cancel_call lead={} phone={} old_slot={} reason={!r}",
        ctx.lead.id, ctx.lead.phone, old_slot, reason,
    )

    try:
        ok = cancel_ready_for_call(ctx.lead, reason=reason)
        if not ok:
            logger.error(
                "[cancel_call] LeadMe cancel push failed for lead {}",
                ctx.lead.id,
            )
    except Exception:
        logger.exception(
            "[cancel_call] LeadMe cancel push RAISED for lead {}",
            ctx.lead.id,
        )

    # Reset local state either way -- the lead-facing reality is more
    # important than the CRM sync (we can retry the CRM manually).
    repository.update_lead_metadata(
        ctx.session, ctx.lead, preferred_call_slot=None,
    )
    repository.update_funnel_stage(ctx.session, ctx.lead, FunnelStage.warm)

    return (
        "התיאום בוטל, המצב חזר לשלב 'warm'. "
        "כעת התנצל בקצרה מול הליד ושאל אותו על החלון הנכון "
        "(9-12 / 12-15 / 15-18). אל תקבע שיחה בהודעה הזאת - חכה לתשובתו."
    )


ALL_TOOLS = [
    search_knowledge,
    classify_lead,
    send_video,
    recommend_video,
    schedule_call,
    cancel_call,
]
