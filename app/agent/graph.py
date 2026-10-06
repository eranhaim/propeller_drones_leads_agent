"""LangChain tool-calling agent + high-level ``handle_message`` entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from loguru import logger

import re

from app.agent.classifier import describe_state
from app.agent.context import AgentContext, VideoSend, use_context
from app.agent.memory import (
    describe_conversation_state,
    non_redundant_continuation,
    record_assistant_turn,
    record_user_turn,
    remove_repeated_outbound_content,
)
from app.agent.prompts import render_system_prompt
from app.agent.tools import ALL_TOOLS, queue_video, record_video_delivery
from app.config import get_settings
from app.crm.client import mark_call_window, mark_engaged_no_book, mark_ready_for_call
from app.crm.levels import classify_engagement
from app.db import repository
from app.db.models import FunnelStage, FamiliarityLevel, Lead, MessageRole
from app.db.session import session_scope
from app.videos.catalog import Video, recommend


HISTORY_LIMIT = 30


@dataclass(frozen=True)
class AgentTurn:
    """Text plus media queued for ordered WhatsApp delivery."""

    reply: str
    video_sends: tuple[VideoSend, ...] = ()

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
    r"^\s*אם\s+תרצ[הי]\s+לשמוע\s+עוד.*כאן.*$",
    r"^\s*אני\s+כאן\s+(?:בשבילך|לעזור|לרשותך|להסביר)\b.*$",
    r"^\s*אני\s+זמין(?:ה)?\b.*$",
    r"^\s*מוזמן(?:ת)?\s+לפנות\b.*$",
    r"^\s*מקווה\s+שעזרתי\b.*$",
    r"^\s*אשמח\s+לעזור\b.*$",
    r"^\s*בשמחה\s+אענה\b.*$",
    r"^\s*תרגיש(?:י)?\s+חופשי\b.*$",
]
_FILLER_RE = re.compile("|".join(_FILLER_PATTERNS))

# The same filler, as bare phrases, for the sentence-level pass below. Kept as
# its own list rather than derived from _FILLER_PATTERNS: two short explicit
# lists are easier to read and to extend than one list plus string surgery.
_FILLER_SENTENCE_PATTERNS = [
    r"אם\s+יש\s+לך\s+(?:עוד\s+)?שאלות",
    r"אם\s+תרצ[הי]\s+לשמוע\s+עוד.*כאן",
    r"אני\s+כאן\s+(?:בשבילך|לעזור|לרשותך|להסביר)",
    r"אני\s+זמינ",
    r"מוזמנ(?:ת)?\s+לפנות",
    r"מקווה\s+שעזרתי",
    r"אשמח\s+לעזור",
    r"בשמחה\s+אענה",
    r"תרגיש(?:י)?\s+חופשי",
]
_FILLER_SENTENCE_RE = re.compile("|".join(_FILLER_SENTENCE_PATTERNS))

# Sentence boundary: end punctuation followed by whitespace. The capturing
# group keeps the whitespace in the split result so the line can be rebuilt
# unchanged when nothing is dropped.
_SENTENCE_SPLIT_RE = re.compile(r"((?<=[.!?])\s+)")


_HEBREW_CHAR_RE = re.compile(r"[\u0590-\u05FF]")
_VIDEO_WORD_RE = re.compile(r"[\u0590-\u05FF]{2,}")
_VIDEO_DELIVERY_RE = re.compile(
    r"(?:שלח(?:תי|נו)|מצורף|הנה|קיבלת).{0,30}"
    r"(?:סרטון|קישור|וידאו)"
)
# The LLM often writes "I'll send you a video" but never calls
# send_video, so the lead is promised media that never arrives. If the final
# reply promises a video and none was sent this turn, the safety net below
# delivers one. Send verbs (future or "here is") near a media noun; a bare
# "we have a video on X" (no send verb) is informational and won't match.
_VIDEO_PROMISE_RE = re.compile(
    r"(?:אשלח|נשלח|שולח(?:ת)?|אעביר|מעביר(?:ה)?|אשתף|אצרף|שלחתי|שלחנו|הנה|מצורף)"
    r".{0,30}"
    r"(?:סרטון|וידאו|קליפ|הדרכה|הרצאה)"
)
_VIDEO_STOP_WORDS = {
    "את",
    "אתה",
    "אתם",
    "אני",
    "הוא",
    "היא",
    "זה",
    "זאת",
    "הזה",
    "הזאת",
    "של",
    "עם",
    "על",
    "אל",
    "גם",
    "כל",
    "לא",
    "לי",
    "לך",
    "לכם",
    "מתי",
    "מה",
    "אם",
    "כי",
    "כדי",
    "כבר",
}


# Refusal phrases a lead uses to end the conversation. Measured on two months
# of production conversations: 45 leads said one of these and 21 of them were
# nudged again afterwards, because ``not_relevant`` is only set when the LLM
# remembers to call ``mark_not_relevant``. Detecting the refusal here makes
# the follow-up scheduler skip them regardless of what the LLM did.
#
# Only applied to SHORT messages: "לא מעוניין בקורס אלא בשירות" is a redirect,
# not a refusal. "לא כרגע" is also ambiguous and remains L2.
_REFUSAL_MAX_CHARS = 80
_REFUSAL_PATTERNS = [
    r"לא\s+מעוני",
    r"לא\s+מעניין",
    r"לא\s+רלוונטי",
    r"לא\s+תודה",
    r"תודה\s+אבל\s+לא",
    r"תפסיק|הפסיקו|תפסיקו",
    r"תסיר|הסר\s+אותי|להסיר\s+אותי",
    r"אל\s+תשלח",
]
_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS))
_COURSE_PRICE_RE = re.compile(
    r"(?:מחיר|כמה\s+עולה|עלות|תשלום).{0,40}(?:קורס|מסלול|לימוד)|"
    r"(?:קורס|מסלול|לימוד).{0,40}(?:מחיר|כמה\s+עולה|עלות|תשלום)"
)
_BUSINESS_INTEREST_RE = re.compile(
    r"(?:לפתוח|להקים).{0,20}(?:עסק|חברה)|"
    r"(?:עצמאי|פרילנס|עסק).{0,30}(?:רחפן|צילום|מיפוי|אבטחה|סולאר)"
)
_BOOKED_SLOT_RE = re.compile(r"(?<!\d)(9\s*-\s*12|12\s*-\s*15|15\s*-\s*18)(?!\d)")


def _is_refusal(text: str) -> bool:
    """Return True if the lead's message is a short, explicit 'stop' message."""
    trimmed = (text or "").strip()
    if not trimmed or len(trimmed) > _REFUSAL_MAX_CHARS:
        return False
    # "לא מעוניין בקורס אלא בשירות" redirects to a different business line;
    # it is not consent to stop all contact.
    if re.search(r"לא\s+מעוני.{0,40}\bאלא\b", trimmed):
        return False
    # "Not interested in a call right now, only information" is an explicit
    # request to stay in the conversation, not an opt-out from Propeller.
    if re.search(
        r"לא\s+מעוני.{0,35}(?:שיחה|כרגע).{0,35}(?:מידע|אינפורמציה)",
        trimmed,
    ):
        return False
    return bool(_REFUSAL_RE.search(trimmed))


def _booked_slot_in_message(text: str) -> Optional[str]:
    match = _BOOKED_SLOT_RE.search(text or "")
    return re.sub(r"\s+", "", match.group(1)) if match else None


def _capture_booked_slot(session, lead: Lead, text: str) -> Optional[str]:
    """Update an already-booked ``any`` call when the lead gives a real slot."""
    if lead.funnel_stage != FunnelStage.handed_off:
        return None
    current_slot = (lead.lead_metadata or {}).get("preferred_call_slot")
    if current_slot != "any":
        return None
    slot = _booked_slot_in_message(text)
    if slot is None:
        return None

    repository.update_lead_metadata(session, lead, preferred_call_slot=slot)
    try:
        mark_call_window(lead, slot, session=session)
    except Exception:
        logger.exception(
            "[schedule_call] failed to sync updated slot {!r} for lead {}",
            slot,
            lead.id,
        )
    logger.info(
        "[schedule_call] captured slot {!r} after booking for lead {}",
        slot,
        lead.id,
    )
    return slot


def _ensure_course_price_advisor(reply: str, user_text: str) -> str:
    """Keep the required human-advisor handoff in course-price answers."""
    if not _COURSE_PRICE_RE.search(user_text or "") or "יועץ" in (reply or ""):
        return reply
    suffix = "יועץ לימודים יוכל לדייק לך את המחיר לפי המסלול המתאים."
    return f"{reply.rstrip()} {suffix}".strip()


def _ensure_business_shop_link(
    reply: str,
    user_text: str,
    session_messages,
) -> str:
    """Mention the shop once when a lead plans a drone-services business."""
    if not _BUSINESS_INTEREST_RE.search(user_text or ""):
        return reply
    if "propeller-drones.shop" in (reply or ""):
        return reply
    if any(
        "propeller-drones.shop" in (message.content or "")
        for message in session_messages
        if message.role == MessageRole.assistant
    ):
        return reply
    suffix = "לציוד לעסק אפשר לראות את החנות שלנו: https://propeller-drones.shop/"
    return f"{reply.rstrip()} {suffix}".strip()


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
    """Drop trailing filler sign-offs the customer flagged as annoying."""
    if not reply:
        return reply
    lines = reply.splitlines()
    # Trim from the end while trailing lines are filler or empty. Don't touch
    # earlier lines -- if a real informative line happens to look like filler
    # (unlikely) we'd rather keep it than lose real content.
    while lines and (not lines[-1].strip() or _FILLER_RE.match(lines[-1])):
        lines.pop()
    if lines:
        lines[-1] = _strip_trailing_filler_sentence(lines[-1])
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines).rstrip()


def _strip_trailing_filler_sentence(line: str) -> str:
    """Drop filler appended to the END of an otherwise real sentence.

    The line-level pass only catches filler that got its own line. In practice
    the LLM writes it inline -- "המחיר תלוי במסלול. אם יש לך שאלות נוספות אני
    כאן!" -- which is why 20% of production replies still ended with a
    sign-off. Only the final sentence is examined, and only a whole sentence
    is removed, so real content is never cut mid-thought.
    """
    # parts alternates [sentence, separator, sentence, ...], so dropping a
    # sentence also drops the separator in front of it. The length guard keeps
    # at least one real sentence.
    parts = _SENTENCE_SPLIT_RE.split(line)
    while len(parts) > 2 and _FILLER_SENTENCE_RE.search(parts[-1]):
        parts.pop()
        parts.pop()
    return "".join(parts).rstrip()


# WhatsApp does NOT render markdown. The LLM falls back to markdown syntax
# anyway ('[label](url)', '**bold**', '## heading') and the lead sees the raw
# brackets and asterisks. Links were fixed first; bold was not, and it kept
# growing (2.7% of replies in July, 5.0% in August). WhatsApp's own bold is a
# SINGLE asterisk, so '**text**' becomes '*text*' rather than being deleted.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD_URL_RE = re.compile(r"\*\*(https?://\S+?)\*\*")
_BOLD_EMAIL_RE = re.compile(r"\*\*([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})\*\*")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+", re.M)


def _strip_markdown(reply: str) -> str:
    """Rewrite markdown syntax WhatsApp does not render into plain text."""
    if not reply:
        return reply
    # [text](url) -> url (drop the label; label is usually the same as the
    # URL anyway, and WhatsApp will linkify the bare URL cleanly).
    reply = _MD_LINK_RE.sub(r"\2", reply)
    # URLs and emails lose their asterisks entirely -- bolding them adds
    # nothing and breaks WhatsApp's auto-linking.
    reply = _BOLD_URL_RE.sub(r"\1", reply)
    reply = _BOLD_EMAIL_RE.sub(r"\1", reply)
    reply = _BOLD_RE.sub(r"*\1*", reply)
    reply = _HEADING_RE.sub("", reply)
    return reply


def _video_words(text: str) -> set[str]:
    """Return content words used to compare a reply with a sent caption."""
    return {
        word
        for word in _VIDEO_WORD_RE.findall((text or "").lower())
        if word not in _VIDEO_STOP_WORDS
    }


def _is_video_delivery_duplicate(sentence: str, video_sends: list[VideoSend]) -> bool:
    """Return True when a sentence only repeats a video delivery/caption."""
    sentence_words = _video_words(sentence)
    if not sentence_words:
        return False

    for sent in video_sends:
        sent_words = _video_words(f"{sent.title} {sent.caption}")
        shared_words = sentence_words & sent_words
        overlap = len(shared_words) / len(sentence_words)
        repeats_delivery = bool(_VIDEO_DELIVERY_RE.search(sentence))

        # A clear "I sent you the video/link" sentence is always a
        # duplicate after the tool has delivered it. Otherwise require most
        # of the sentence to repeat the actual title/caption, so a genuinely
        # new follow-up remains visible to the lead.
        if repeats_delivery or (
            len(sentence_words) >= 3
            and len(shared_words) >= 2
            and overlap >= 0.6
        ):
            return True
    return False


def _remove_video_delivery_duplicates(
    reply: str,
    video_sends: list[VideoSend],
) -> str:
    """Remove LLM text that repeats media already sent by ``send_video``."""
    if not reply or not video_sends:
        return reply

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply)
        if sentence.strip()
    ]
    kept = [
        sentence
        for sentence in sentences
        if not _is_video_delivery_duplicate(sentence, video_sends)
    ]
    if len(kept) == len(sentences):
        return reply

    logger.info(
        "[video-delivery] removed duplicate final reply after sending {}",
        ", ".join(sent.video_id for sent in video_sends),
    )
    return " ".join(kept)


@lru_cache(maxsize=1)
def _model() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.openai_chat_model,
        api_key=settings.openai_api_key,
        temperature=0.4,
        # A permanently stalled model request blocks GreenAPI's synchronous
        # dispatcher and leaves all newer WhatsApp messages queued.
        timeout=90.0,
        max_retries=2,
    )


@lru_cache(maxsize=1)
def _agent():
    """Cached LangGraph ReAct-style tool-calling agent."""
    return create_react_agent(model=_model(), tools=ALL_TOOLS)


SESSION_RESET_DAYS = 7


def _history_as_messages(lead: Lead, session) -> List[BaseMessage]:
    """Turn stored DB messages into LangChain messages, oldest first.

    Only messages from the current session are included — i.e. those created
    after ``lead_metadata["session_reset_at"]`` if a reset has occurred.
    """
    stored = _current_session_messages(lead, session)
    msgs: List[BaseMessage] = []
    for m in stored:
        if m.role == MessageRole.user:
            msgs.append(HumanMessage(content=m.content))
        elif m.role == MessageRole.assistant:
            msgs.append(AIMessage(content=m.content))
    return msgs


def _current_session_messages(lead: Lead, session):
    """Load durable history visible to both the model and memory safeguards."""
    from datetime import datetime, timezone
    reset_str = (lead.lead_metadata or {}).get("session_reset_at")
    after_dt = None
    if reset_str:
        try:
            after_dt = datetime.fromisoformat(reset_str)
        except ValueError:
            pass

    return repository.recent_messages(
        session,
        lead,
        limit=HISTORY_LIMIT,
        after_dt=after_dt,
    )


def _should_reset_session(lead: Lead) -> bool:
    """Return True if this lead's session has been idle for SESSION_RESET_DAYS.

    Idle is measured from the lead's OWN last message, not ``last_message_at``
    -- that column is bumped by our nudges too, so a lead answering a nudge
    used to be greeted with the first-contact opener as if we had never
    spoken.
    """
    from datetime import datetime, timezone, timedelta

    messages = lead.messages or []
    last_user = max(
        (m for m in messages if m.role == MessageRole.user),
        key=lambda m: m.created_at, default=None,
    )
    if last_user is None:
        # Opener only, no conversation yet -- there is nothing to reset.
        return False

    last_assistant = max(
        (m for m in messages if m.role == MessageRole.assistant),
        key=lambda m: m.created_at, default=None,
    )
    if last_assistant is not None and (last_assistant.msg_metadata or {}).get("nudge"):
        # They are answering our nudge -- keep the thread rather than
        # greeting them as a stranger in reply to our own invitation.
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_RESET_DAYS)
    return last_user.created_at < cutoff


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


def _result(
    reply: str,
    ctx: Optional[AgentContext],
    defer_video_delivery: bool,
) -> str | AgentTurn:
    if defer_video_delivery:
        return AgentTurn(
            reply=reply,
            video_sends=tuple(ctx.video_sends_this_turn) if ctx else (),
        )
    return reply


def _dispatch_pending_videos(session, lead: Lead, ctx: AgentContext) -> None:
    """Deliver queued media immediately for non-WhatsApp callers.

    Production passes ``defer_video_delivery=True`` and dispatches after the
    text reply. The immediate branch preserves the small public API used by
    offline scripts and tests.
    """
    if not ctx.video_sends_this_turn or ctx.send_video is None:
        return
    from app.videos.catalog import get_video

    for delivery in ctx.video_sends_this_turn:
        video = get_video(delivery.video_id)
        if video is None:
            logger.error(
                "[send_video] queued unknown video {!r}; not dispatching",
                delivery.video_id,
            )
            continue
        try:
            ctx.send_video(video, delivery.caption)
        except Exception:
            logger.exception(
                "[send_video] failed to dispatch queued video {!r} for lead {}",
                delivery.video_id,
                lead.id,
            )
            continue
        record_video_delivery(session, lead, video, delivery)


def handle_message(
    phone: str,
    text: str,
    sender_name: Optional[str] = None,
    send_video_fn: Optional[Callable[[Video, Optional[str]], None]] = None,
    inbound_message_id: Optional[str] = None,
    defer_video_delivery: bool = False,
) -> str | AgentTurn:
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
    push_not_relevant = False
    with session_scope() as session:
        lead = repository.get_or_create_lead(session, phone=phone, name=sender_name)
        if (
            inbound_message_id
            and repository.has_inbound_message_id(session, lead, inbound_message_id)
        ):
            logger.info(
                "[inbound-dedup] skipping already processed message {} for lead {}",
                inbound_message_id,
                lead.id,
            )
            return _result("", None, defer_video_delivery)

        # Session reset: if the lead has been idle for SESSION_RESET_DAYS, treat
        # them as brand-new (clears stage/familiarity/videos/metadata except
        # LeadMe IDs) so they get a fresh opener and can receive videos again.
        if _should_reset_session(lead):
            logger.info(
                "[session-reset] lead {} idle since {}, resetting session",
                lead.id, lead.last_message_at,
            )
            repository.reset_lead_session(session, lead)

        md_before = dict(lead.lead_metadata or {})
        # An explicit refusal is terminal in LeadMe. A later inbound message
        # is a renewed conversation, so normal engagement classification may
        # resume on that turn.
        refused_now = _is_refusal(text)
        if md_before.get("not_relevant") and not refused_now:
            md_before.pop("not_relevant")
            md_before.pop("leadme_relevance", None)
            pending = list(md_before.get("leadme_push_pending") or [])
            pending = [item for item in pending if item.get("kind") != "status"]
            if pending:
                md_before["leadme_push_pending"] = pending
            else:
                for key in (
                    "leadme_push_pending",
                    "leadme_push_next_attempt_at",
                    "leadme_push_attempts",
                    "leadme_push_queued_at",
                ):
                    md_before.pop(key, None)
            lead.lead_metadata = md_before
            session.flush()
        elif refused_now and not md_before.get("not_relevant"):
            logger.info("[not-relevant] lead {} refused: {!r}", lead.id, text[:60])
            md_before["not_relevant"] = True
            md_before["leadme_relevance"] = "not_relevant"
            lead.lead_metadata = md_before
            session.flush()
            push_not_relevant = True
        repository.add_message(
            session,
            lead,
            MessageRole.user,
            text,
            metadata=(
                {"greenapi_message_id": inbound_message_id}
                if inbound_message_id
                else None
            ),
        )
        record_user_turn(session, lead, text)
        lead_id = lead.id

    # CRM changes use their own transaction so a CRM failure never blocks the
    # user-facing reply. An explicit opt-out wins over engagement levels.
    if push_not_relevant:
        try:
            from app.crm.client import mark_not_relevant
            with session_scope() as s_relevance:
                l_relevance = s_relevance.get(Lead, lead_id)
                if l_relevance is not None:
                    mark_not_relevant(
                        l_relevance,
                        note="explicit WhatsApp opt-out",
                        session=s_relevance,
                    )
        except Exception:
            logger.exception(
                "[not-relevant] LeadMe status queue failed for lead {}", lead_id,
            )

    if not refused_now:
        try:
            with session_scope() as s_lvl:
                l_lvl = s_lvl.get(Lead, lead_id)
                if l_lvl is not None:
                    decision = classify_engagement(l_lvl)
                    if decision.level == 1:
                        mark_ready_for_call(
                            l_lvl,
                            note=f"classification={decision.reason}",
                            session=s_lvl,
                        )
                    elif decision.level == 2:
                        mark_engaged_no_book(
                            l_lvl,
                            note=f"classification={decision.reason}",
                            session=s_lvl,
                        )
                    elif decision.level == 3:
                        from app.crm.client import mark_no_reply
                        mark_no_reply(
                            l_lvl,
                            note=f"classification={decision.reason}",
                            session=s_lvl,
                        )
        except Exception:
            logger.exception("[level-push] classification push failed for lead {}", lead_id)
    # ---- Transaction 2: run the agent and persist the reply. ---------------
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            logger.error("Lead {} vanished between txns; aborting", lead_id)
            return _result("", None, defer_video_delivery)

        captured_booked_slot = _capture_booked_slot(session, lead, text)
        session_messages = _current_session_messages(lead, session)
        system_prompt = render_system_prompt(
            describe_state(lead)
            + "\n\n"
            + describe_conversation_state(lead, session_messages)
        )
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
                record_assistant_turn(session, lead, fallback)
                return _result(fallback, ctx, defer_video_delivery)

            reply = _extract_reply(result)
            if not reply and not ctx.video_sends_this_turn:
                reply = "רגע, אני חושב על זה... אפשר לחדד קצת מה מעניין אותך?"

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
        reply = _strip_markdown(reply)
        reply = _ensure_course_price_advisor(reply, text)
        reply = _ensure_business_shop_link(reply, text, session_messages)
        _enforce_video_promise(session, lead, ctx, reply, text)
        reply = _remove_video_delivery_duplicates(reply, ctx.video_sends_this_turn)
        reply, removed_duplicate = remove_repeated_outbound_content(
            reply,
            session_messages,
        )
        if removed_duplicate:
            logger.warning(
                "[conversation-memory] removed repeated outbound content for lead {}",
                lead.id,
            )

        _enforce_booking_promise(session, lead, reply)

        if not reply and not ctx.video_sends_this_turn:
            reply = non_redundant_continuation(lead, session_messages)
        if captured_booked_slot:
            reply = (
                f"מעולה, עדכנתי את היועץ לחלון {captured_booked_slot}. "
                "היועץ ייצור איתך קשר בזמן הזה."
            )
        if reply:
            repository.add_message(session, lead, MessageRole.assistant, reply)
            record_assistant_turn(session, lead, reply)
        if not defer_video_delivery:
            _dispatch_pending_videos(session, lead, ctx)
        return _result(reply, ctx, defer_video_delivery)


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
    slot = md.get("preferred_call_slot")

    logger.warning(
        "[booking-safety-net] Reply for lead {} promises a call but stage is "
        "{!r}. slot={}.",
        lead.id, lead.funnel_stage.value, slot or "not captured yet",
    )

    # No slot captured -- book with "any" rather than bail. The reply already
    # promised the lead a call; leaving them out of the CRM because we are
    # missing a time window is exactly how warm leads get lost.
    if not slot:
        slot = "any"
        repository.update_lead_metadata(session, lead, preferred_call_slot=slot)
        logger.warning(
            "[booking-safety-net] No slot captured for lead {} -- pushing "
            "with slot='any' to keep the promise.",
            lead.id,
        )

    decision = classify_engagement(lead)
    try:
        if decision.level == 1:
            ok = mark_ready_for_call(
                lead, note=f"safety-net auto-push (slot={slot})", session=session,
            )
        else:
            ok = mark_engaged_no_book(
                lead, note=f"safety-net auto-push (slot={slot})", session=session,
            )
        mark_call_window(lead, slot, session=session)
        if ok:
            repository.update_funnel_stage(session, lead, FunnelStage.handed_off)
            logger.info(
                "[booking-safety-net] Auto-pushed lead {} to LeadMe (L{}); "
                "stage -> handed_off", lead.id, decision.level,
            )
        else:
            logger.error(
                "[booking-safety-net] engagement push returned False for lead {} "
                "-- lead was promised a call but LeadMe push failed",
                lead.id,
            )
    except Exception:
        logger.exception(
            "[booking-safety-net] engagement push raised for lead {} "
            "-- lead was promised a call but LeadMe push failed",
            lead.id,
        )


def _enforce_video_promise(
    session,
    lead: Lead,
    ctx: AgentContext,
    reply: str,
    user_text: str,
) -> None:
    """If the reply promises a video but none was sent, send one.

    Prompt-only guardrails are not enough: the LLM often tells a lead "I'll
    send you a video" and never calls ``send_video``, so the lead is
    left waiting for media that never arrives. This is a belt-and-suspenders
    safety net, mirroring ``_enforce_booking_promise``. If the outgoing text
    promises a video and the agent didn't actually send one this turn, we pick
    an unsent video only when the lead's message and the reply match its
    catalog cues. This deliberately does not fall back to a generic video.
    """
    if ctx.video_sends_this_turn:
        return
    if not _VIDEO_PROMISE_RE.search(reply or ""):
        return
    if ctx.send_video is None:
        logger.warning(
            "[video-safety-net] lead {} was promised a video but no sender is "
            "configured -- cannot deliver", lead.id,
        )
        return

    exclude = list(lead.videos_sent or []) + list(ctx.videos_sent_this_turn)
    video = recommend(
        familiarity=lead.familiarity_level.value,
        topics_context=[user_text, reply],
        exclude_ids=exclude,
    )
    if video is None:
        logger.warning(
            "[video-safety-net] lead {} was promised a video but no matching "
            "unsent video is available (excluded={})", lead.id, exclude,
        )
        return

    logger.warning(
        "[video-safety-net] reply for lead {} promises a video but send_video "
        "never fired; queueing {!r}", lead.id, video.id,
    )
    try:
        queue_video(ctx, video, None)
    except Exception:
        logger.exception(
            "[video-safety-net] failed to queue promised video {!r} for lead {}",
            video.id, lead.id,
        )


# ---------------------------------------------------------------------------
# Simulator — runs the agent in-memory, no CRM/WhatsApp side-effects
# ---------------------------------------------------------------------------

from contextvars import ContextVar as _ContextVar

# In-memory store: session_id -> {"history": [...], "state": {...}}
_sim_sessions: Dict[str, dict] = {}

# Per-invocation mutable state dict, written to by stub tools.
_sim_state_var: _ContextVar[Optional[dict]] = _ContextVar("_sim_state", default=None)

_INITIAL_STATE = {
    "familiarity": "unknown",
    "stage": "new",
    "intent": None,
    "industry": None,
    "preferred_call_slot": None,
    "has_experience": None,
    "videos_sent": [],
    "call_scheduled": False,
}


def _sim_session(session_id: str) -> dict:
    if session_id not in _sim_sessions:
        import copy
        _sim_sessions[session_id] = {
            "history": [],
            "state": copy.deepcopy(_INITIAL_STATE),
        }
    return _sim_sessions[session_id]


# Stub tools — write to the shared state dict via context var.
@tool
def _sim_classify_lead(
    familiarity: Optional[str] = None,
    stage: Optional[str] = None,
    intent: Optional[str] = None,
    industry: Optional[str] = None,
    preferred_call_slot: Optional[str] = None,
    has_experience: Optional[bool] = None,
) -> str:
    """Update lead classification (simulator — no DB write)."""
    state = _sim_state_var.get()
    parts = []
    if familiarity:
        state["familiarity"] = familiarity
        parts.append(f"היכרות={familiarity}")
    if stage:
        state["stage"] = stage
        parts.append(f"שלב={stage}")
    if intent:
        state["intent"] = intent
        parts.append(f"כוונה={intent}")
    if industry:
        state["industry"] = industry
        parts.append(f"תעשייה={industry}")
    if preferred_call_slot:
        state["preferred_call_slot"] = preferred_call_slot
        parts.append(f"חלון={preferred_call_slot}")
    if has_experience is not None:
        state["has_experience"] = has_experience
        parts.append(f"ניסיון={'כן' if has_experience else 'לא'}")
    return "[סימולטור] עודכן: " + (", ".join(parts) or "אין שינויים")


@tool
def _sim_send_video(video_id: str, caption: Optional[str] = None) -> str:
    """Send a video to the lead (simulator — no WhatsApp send)."""
    from app.videos.catalog import get_video
    state = _sim_state_var.get()
    v = get_video(video_id)
    if v is None:
        return f"[סימולטור] שגיאה: אין סרטון {video_id}"
    if video_id not in state["videos_sent"]:
        state["videos_sent"].append(video_id)
    return f"[סימולטור] הסרטון '{v.title}' היה נשלח"


@tool
def _sim_recommend_video(topics_context: Optional[str] = None) -> str:
    """Recommend a video (simulator)."""
    from app.videos.catalog import recommend
    state = _sim_state_var.get()
    v = recommend(
        familiarity=state.get("familiarity", "unknown"),
        topics_context=[topics_context] if topics_context else [],
        exclude_ids=state.get("videos_sent", []),
    )
    if v is None:
        return "[סימולטור] אין המלצת סרטון."
    return f"[סימולטור] מומלץ: {v.id} - {v.title}"


@tool
def _sim_schedule_call(
    summary: Optional[str] = None,
    preferred_call_slot: Optional[str] = None,
) -> str:
    """Schedule a call (simulator — no CRM push)."""
    state = _sim_state_var.get()
    if preferred_call_slot:
        state["preferred_call_slot"] = preferred_call_slot
    booked_without_slot = not state.get("preferred_call_slot")
    if booked_without_slot:
        state["preferred_call_slot"] = "any"
    slot = state["preferred_call_slot"]
    state["call_scheduled"] = True
    state["stage"] = "handed_off"
    if booked_without_slot:
        return (
            "[סימולטור] שיחה הייתה מתואמת (חלון: any). כעת אמור ללקוח "
            "שהיועץ ייצור איתו קשר, ושאל פעם אחת איזה חלון שעות מועדף עליו."
        )
    return f"[סימולטור] שיחה הייתה מתואמת (חלון: {slot}). אין push ל-CRM בסימולטור."


@tool
def _sim_cancel_call(reason: Optional[str] = None) -> str:
    """Cancel a call (simulator — no CRM push)."""
    state = _sim_state_var.get()
    state["call_scheduled"] = False
    state["preferred_call_slot"] = None
    state["stage"] = "warm"
    return "[סימולטור] תיאום השיחה בוטל."


_SIM_TOOLS = [
    next(t for t in ALL_TOOLS if t.name == "search_knowledge"),
    _sim_classify_lead,
    _sim_send_video,
    _sim_recommend_video,
    _sim_schedule_call,
    _sim_cancel_call,
]


@lru_cache(maxsize=1)
def _sim_agent():
    return create_react_agent(model=_model(), tools=_SIM_TOOLS)


def simulate_message(session_id: str, text: str) -> dict:
    """Run the agent on *text* in a sandboxed in-memory session.

    Returns {"reply": str, "state": dict}.
    """
    import copy
    sess = _sim_session(session_id)
    history: List[BaseMessage] = sess["history"]
    state: dict = sess["state"]

    # Rebuild a fake lead from the current sim state so the system prompt
    # reflects what the bot has learned so far.
    fake_lead = Lead()
    fake_lead.id = 0
    fake_lead.phone = f"sim_{session_id}"
    fake_lead.name = "סימולטור"
    fake_lead.familiarity_level = FamiliarityLevel(state.get("familiarity", "unknown"))
    fake_lead.funnel_stage = FunnelStage(state.get("stage", "new"))
    fake_lead.lead_metadata = {
        k: state[k]
        for k in ("intent", "industry", "preferred_call_slot", "has_experience")
        if state.get(k) is not None
    }
    fake_lead.videos_sent = list(state.get("videos_sent", []))
    fake_lead.messages = []

    system_prompt = render_system_prompt(describe_state(fake_lead))
    input_messages: List[BaseMessage] = [SystemMessage(content=system_prompt)]
    input_messages.extend(history)
    input_messages.append(HumanMessage(content=text))

    # Give the stub tools access to the live state dict via context var.
    token = _sim_state_var.set(state)
    ctx = AgentContext(session=None, lead=fake_lead, send_video=None)  # type: ignore[arg-type]
    try:
        with use_context(ctx):
            result = _sim_agent().invoke({"messages": input_messages})
    except Exception:
        logger.exception("[simulator] Agent invocation failed session={}", session_id)
        _sim_state_var.reset(token)
        return {"reply": "שגיאה פנימית בסימולטור — בדוק את הלוגים.", "state": copy.deepcopy(state)}
    finally:
        _sim_state_var.reset(token)

    reply = _extract_reply(result) or "..."
    reply = _strip_filler(reply)
    reply = _strip_markdown(reply)

    history.append(HumanMessage(content=text))
    history.append(AIMessage(content=reply))

    return {"reply": reply, "state": copy.deepcopy(state)}


def clear_simulation(session_id: str) -> None:
    """Wipe the in-memory session (history + state)."""
    _sim_sessions.pop(session_id, None)
