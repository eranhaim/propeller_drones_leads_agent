"""LangChain tool-calling agent + high-level ``handle_message`` entrypoint."""

from __future__ import annotations

from functools import lru_cache
from typing import Callable, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from loguru import logger

import re

from app.agent.classifier import describe_state
from app.agent.context import AgentContext, use_context
from app.agent.prompts import render_system_prompt
from app.agent.tools import ALL_TOOLS
from app.config import get_settings
from app.crm.client import mark_ready_for_call
from app.db import repository
from app.db.models import FunnelStage, Lead, MessageRole
from app.db.session import session_scope
from app.videos.catalog import Video


HISTORY_LIMIT = 30

# Phrases the LLM uses when it *claims* it booked a call. If we see any of
# these in the outgoing reply but the tool never actually fired (funnel_stage
# still not handed_off), we auto-invoke schedule_call to keep our promise to
# the lead. Prevents the "bot promised a call, sales team never got it" bug.
_BOOKING_PROMISE_PATTERNS = [
    r"קבעתי\s+לך\s+שיחה",
    r"תיאמתי\s+לך\s+שיחה",
    r"קבענו\s+לך\s+שיחה",
    r"תיאמנו\s+לך\s+שיחה",
    r"הנציג\s+(?:שלנו\s+)?ייצור\s+איתך\s+קשר",
    r"נציג\s+(?:שלנו\s+)?ייצור\s+איתך\s+קשר",
    r"אעדכן\s+את\s+הנציג",
    r"נציג\s+יחזור\s+אלי[יך]",
]
_BOOKING_PROMISE_RE = re.compile("|".join(_BOOKING_PROMISE_PATTERNS))


@lru_cache(maxsize=1)
def _model() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.openai_chat_model,
        api_key=settings.openai_api_key,
        temperature=0.4,
    )


@lru_cache(maxsize=1)
def _agent():
    """Cached LangGraph ReAct-style tool-calling agent."""
    return create_react_agent(model=_model(), tools=ALL_TOOLS)


def _history_as_messages(lead: Lead, session) -> List[BaseMessage]:
    """Turn stored DB messages into LangChain messages, oldest first."""
    stored = repository.recent_messages(session, lead, limit=HISTORY_LIMIT)
    msgs: List[BaseMessage] = []
    for m in stored:
        if m.role == MessageRole.user:
            msgs.append(HumanMessage(content=m.content))
        elif m.role == MessageRole.assistant:
            msgs.append(AIMessage(content=m.content))
    return msgs


def _extract_reply(result: dict) -> str:
    """Get the final assistant text from an agent result."""
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                text = "".join(parts).strip()
                if text:
                    return text
    return ""


def handle_message(
    phone: str,
    text: str,
    sender_name: Optional[str] = None,
    send_video_fn: Optional[Callable[[Video, Optional[str]], None]] = None,
) -> str:
    """Full pipeline for one inbound WhatsApp message.

    1. Load or create the lead.
    2. Append the inbound message to history.
    3. Build the agent's input (system prompt + history + new user turn).
    4. Invoke the tool-calling agent with per-request context.
    5. Persist the assistant reply and return the outgoing text.
    """
    logger.info("Handle message from {} ({} chars)", phone, len(text))

    with session_scope() as session:
        lead = repository.get_or_create_lead(session, phone=phone, name=sender_name)
        repository.add_message(session, lead, MessageRole.user, text)
        session.flush()

        system_prompt = render_system_prompt(describe_state(lead))
        history_msgs = _history_as_messages(lead, session)

        input_messages: List[BaseMessage] = [SystemMessage(content=system_prompt)]
        input_messages.extend(history_msgs)

        ctx = AgentContext(session=session, lead=lead, send_video=send_video_fn)

        with use_context(ctx):
            try:
                result = _agent().invoke({"messages": input_messages})
            except Exception:
                logger.exception("Agent invocation failed for lead {}", lead.id)
                fallback = (
                    "סליחה, יש לי כרגע בעיה קטנה בצד שלי. "
                    "אני אחזור אליך תוך דקה - או שכבר אפשר לקבוע שיחה עם יועץ?"
                )
                repository.add_message(session, lead, MessageRole.assistant, fallback)
                return fallback

        reply = _extract_reply(result) or (
            "רגע, אני חושב על זה... אפשר לחדד קצת מה מעניין אותך?"
        )

        _enforce_booking_promise(session, lead, reply)

        repository.add_message(session, lead, MessageRole.assistant, reply)
        return reply


def _enforce_booking_promise(session, lead: Lead, reply: str) -> None:
    """If the reply promises a call but no call was actually scheduled, do it.

    Prompt-only guardrails are not enough -- we saw the LLM tell leads "I
    booked you a call" while never calling ``schedule_call``. That leaves
    the lead expecting a rep who will never phone them. This is a
    belt-and-suspenders safety net: if the outgoing text contains any of
    the booking-promise phrases and the lead is not yet handed_off, we
    push to LeadMe here and bump the stage. Loud logging either way so
    we can measure how often the LLM is being sloppy.
    """
    if lead.funnel_stage == FunnelStage.handed_off:
        return
    if not _BOOKING_PROMISE_RE.search(reply or ""):
        return

    md = lead.lead_metadata or {}
    slot = md.get("preferred_call_slot") or "any"

    logger.warning(
        "[booking-safety-net] Reply for lead {} promises a call but stage is "
        "{!r}. Auto-invoking mark_ready_for_call (slot={}).",
        lead.id, lead.funnel_stage.value, slot,
    )

    if not md.get("preferred_call_slot"):
        repository.update_lead_metadata(session, lead, preferred_call_slot="any")

    try:
        ok = mark_ready_for_call(
            lead,
            note=f"safety-net auto-push (slot={slot})",
        )
        if ok:
            repository.update_funnel_stage(session, lead, FunnelStage.handed_off)
            logger.info(
                "[booking-safety-net] Auto-pushed lead {} to LeadMe; stage -> handed_off",
                lead.id,
            )
        else:
            logger.error(
                "[booking-safety-net] mark_ready_for_call returned False for lead {} "
                "-- lead was promised a call but LeadMe push failed",
                lead.id,
            )
    except Exception:
        logger.exception(
            "[booking-safety-net] mark_ready_for_call raised for lead {} "
            "-- lead was promised a call but LeadMe push failed",
            lead.id,
        )
