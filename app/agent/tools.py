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
    תיאוריה"). Pass ``topic`` to filter (courses, service_flight,
    service_washing, about, general).
    """
    logger.info("Tool search_knowledge: query='{}' topic={}", query, topic)
    return search_as_text(query, k=5, topic=topic)


@tool
def classify_lead(
    familiarity: Optional[str] = None,
    stage: Optional[str] = None,
) -> str:
    """Update the lead's classification in the database.

    ``familiarity`` (optional): one of ``beginner``, ``aware``, ``experienced``.
    ``stage`` (optional): one of ``new``, ``engaged``, ``warm``,
    ``ready_for_call``, ``handed_off``.
    Call this whenever new information changes your assessment of the lead.
    Returns the updated state description.
    """
    ctx = current_context()
    logger.info(
        "Tool classify_lead lead={} familiarity={} stage={}",
        ctx.lead.id, familiarity, stage,
    )
    apply_classification(ctx.session, ctx.lead, familiarity=familiarity, stage=stage)
    return (
        f"עודכן. רמת היכרות={ctx.lead.familiarity_level.value}, "
        f"שלב={ctx.lead.funnel_stage.value}."
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
    this when the lead has explicitly said they want to talk to a sales
    rep. ``summary`` is an optional short internal note about the lead
    (in Hebrew).

    After calling this, tell the lead in your next message (in Hebrew,
    friendly tone) that a sales rep will contact them shortly.
    """
    ctx = current_context()
    repository.update_funnel_stage(ctx.session, ctx.lead, FunnelStage.handed_off)
    mark_ready_for_call(ctx.lead, note=summary)
    return (
        "סומן להעברה למכירות והועבר ל-CRM. "
        "כעת ענה ללקוח שנציג ייצור איתו קשר בקרוב."
    )


ALL_TOOLS = [
    search_knowledge,
    classify_lead,
    send_video,
    recommend_video,
    schedule_call,
]
