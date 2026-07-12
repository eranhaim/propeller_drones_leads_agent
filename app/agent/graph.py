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
    r"(?:יועץ|נציג)(?:\s+לימודים)?(?:\s+שלנו)?\s+ייצור\s+איתך\s+קשר",
    r"אעדכן\s+את\s+(?:יועץ|הנציג)",
    r"(?:יועץ|נציג)\s+יחזור\s+אלי[יך]",
]
_BOOKING_PROMISE_RE = re.compile("|".join(_BOOKING_PROMISE_PATTERNS))


# Trailing-filler lines the customer explicitly rejected as "חופר" (annoying).
# The LLM tends to end nearly every reply with one of these — we sanitize
# them out in post-processing as a hard safety net in addition to the prompt
# rule. Applied line-by-line so real content that happens to contain the
# phrase mid-sentence is left alone.
_FILLER_PATTERNS = [
    r"^\s*אם\s+יש\s+לך\s+שאלות\s+נוספות.*$",
    r"^\s*אם\s+יש\s+לך\s+עוד\s+שאלות.*$",
    r"^\s*אני\s+כאן\s+(?:בשבילך|לעזור|לרשותך|להסביר)\b.*$",
    r"^\s*אני\s+זמין(?:ה)?\b.*$",
    r"^\s*מוזמן(?:ת)?\s+לפנות\b.*$",
    r"^\s*מקווה\s+שעזרתי\b.*$",
    r"^\s*אשמח\s+לעזור\b.*$",
    r"^\s*בשמחה\s+אענה\b.*$",
    r"^\s*תרגיש(?:י)?\s+חופשי\b.*$",
]
_FILLER_RE = re.compile("|".join(_FILLER_PATTERNS))


_HEBREW_CHAR_RE = re.compile(r"[\u0590-\u05FF]")


def _looks_like_english(reply: str) -> bool:
    """Return True if the reply is mostly non-Hebrew and long enough to matter.

    Customer flagged: 'הבוט עונה באנגלית כשהלקוח כותב באנגלית'. The FB
    campaign auto-DMs some leads in English, the LLM mirrors the language,
    and the customer wants Hebrew replies always. This heuristic catches
    that case as a hard safety net on top of the prompt rule.
    """
    if not reply:
        return False
    trimmed = reply.strip()
    if len(trimmed) < 20:
        # Very short replies (emojis, "ok") aren't worth re-running for.
        return False
    hebrew_chars = len(_HEBREW_CHAR_RE.findall(trimmed))
    letter_chars = sum(1 for c in trimmed if c.isalpha())
    if letter_chars == 0:
        return False
    return (hebrew_chars / letter_chars) < 0.15


def _strip_filler(reply: str) -> str:
    """Drop trailing filler-line sign-offs the customer flagged as annoying."""
    if not reply:
        return reply
    lines = reply.splitlines()
    # Trim from the end while trailing lines are filler or empty. Don't touch
    # earlier lines -- if a real informative line happens to look like filler
    # (unlikely) we'd rather keep it than lose real content.
    while lines and (not lines[-1].strip() or _FILLER_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines).rstrip()


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

    # ---- Transaction 1: persist the inbound message immediately. -----------
    # If the agent invocation crashes below, we still have a durable record
    # of the user's message in the DB. Losing the message means the sales
    # team has no idea the lead reached out.
    with session_scope() as session:
        lead = repository.get_or_create_lead(session, phone=phone, name=sender_name)
        repository.add_message(session, lead, MessageRole.user, text)
        lead_id = lead.id
    # ---- Transaction 2: run the agent and persist the reply. ---------------
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            logger.error("Lead {} vanished between txns; aborting", lead_id)
            return ""

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
                    "אני אחזור אליך תוך דקה - או שכבר אפשר לקבוע שיחה עם יועץ לימודים?"
                )
                repository.add_message(session, lead, MessageRole.assistant, fallback)
                return fallback

            reply = _extract_reply(result) or (
                "רגע, אני חושב על זה... אפשר לחדד קצת מה מעניין אותך?"
            )

            # Hebrew safety net: if the reply came back mostly in English
            # (or another non-Hebrew script) despite the prompt rule, run
            # the agent ONCE more with an explicit "reply in Hebrew" nudge.
            if _looks_like_english(reply):
                logger.warning(
                    "[hebrew-safety-net] lead {} got non-Hebrew reply "
                    "({} chars); retrying with Hebrew reminder",
                    lead.id, len(reply),
                )
                retry_messages = list(input_messages) + [
                    AIMessage(content=reply),
                    HumanMessage(content=(
                        "תזכורת מערכת: תמיד ענה בעברית בלבד, גם אם הליד "
                        "כתב באנגלית. תכתוב מחדש את התשובה האחרונה שלך "
                        "בעברית תקנית ונקייה, בלי לתרגם לאנגלית ובלי "
                        "לכתוב את שתי השפות."
                    )),
                ]
                try:
                    retry_result = _agent().invoke({"messages": retry_messages})
                    retry_reply = _extract_reply(retry_result)
                    if retry_reply and not _looks_like_english(retry_reply):
                        reply = retry_reply
                        logger.info(
                            "[hebrew-safety-net] retry succeeded for lead {}",
                            lead.id,
                        )
                    else:
                        logger.error(
                            "[hebrew-safety-net] retry still non-Hebrew for "
                            "lead {}; falling back to canned Hebrew reply",
                            lead.id,
                        )
                        reply = (
                            "היי, אצלנו בפרופלור דרונס אנחנו מדברים בעברית 🙂 "
                            "תוכל לספר לי בעברית מה מעניין אותך - קורס, "
                            "רחפן, או שירות מסחרי?"
                        )
                except Exception:
                    logger.exception(
                        "[hebrew-safety-net] retry raised for lead {}",
                        lead.id,
                    )

        reply = _strip_filler(reply)

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
