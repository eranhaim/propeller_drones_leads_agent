"""LangChain tool-calling agent + high-level ``handle_message`` entrypoint."""

from __future__ import annotations

from functools import lru_cache
from typing import Callable, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
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
from app.db.models import FunnelStage, FamiliarityLevel, Lead, MessageRole
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


# WhatsApp does NOT render markdown links. The LLM sometimes falls back
# to markdown syntax anyway ('[label](url)' or '**url**') and the lead
# sees the raw brackets/asterisks. Convert every markdown link to just
# its URL and strip bold-asterisks around URLs.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD_URL_RE = re.compile(r"\*\*(https?://\S+?)\*\*")
_BOLD_EMAIL_RE = re.compile(r"\*\*([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})\*\*")


def _strip_markdown_links(reply: str) -> str:
    """WhatsApp-safe: turn markdown-link and bold-URL syntax into plain URLs."""
    if not reply:
        return reply
    # [text](url) -> url (drop the label; label is usually the same as the
    # URL anyway, and WhatsApp will linkify the bare URL cleanly).
    reply = _MD_LINK_RE.sub(r"\2", reply)
    reply = _BOLD_URL_RE.sub(r"\1", reply)
    reply = _BOLD_EMAIL_RE.sub(r"\1", reply)
    return reply


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


SESSION_RESET_DAYS = 7


def _history_as_messages(lead: Lead, session) -> List[BaseMessage]:
    """Turn stored DB messages into LangChain messages, oldest first.

    Only messages from the current session are included — i.e. those created
    after ``lead_metadata["session_reset_at"]`` if a reset has occurred.
    """
    from datetime import datetime, timezone
    reset_str = (lead.lead_metadata or {}).get("session_reset_at")
    after_dt = None
    if reset_str:
        try:
            after_dt = datetime.fromisoformat(reset_str)
        except ValueError:
            pass

    stored = repository.recent_messages(session, lead, limit=HISTORY_LIMIT, after_dt=after_dt)
    msgs: List[BaseMessage] = []
    for m in stored:
        if m.role == MessageRole.user:
            msgs.append(HumanMessage(content=m.content))
        elif m.role == MessageRole.assistant:
            msgs.append(AIMessage(content=m.content))
    return msgs


def _should_reset_session(lead: Lead) -> bool:
    """Return True if this lead's session has been idle for SESSION_RESET_DAYS."""
    from datetime import datetime, timezone, timedelta
    if lead.last_message_at is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_RESET_DAYS)
    return lead.last_message_at < cutoff


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
    push_level_2 = False
    session_was_reset = False
    with session_scope() as session:
        lead = repository.get_or_create_lead(session, phone=phone, name=sender_name)

        # Session reset: if the lead has been idle for SESSION_RESET_DAYS, treat
        # them as brand-new (clears stage/familiarity/videos/metadata except
        # LeadMe IDs) so they get a fresh opener and can receive videos again.
        if _should_reset_session(lead):
            logger.info(
                "[session-reset] lead {} idle since {}, resetting session",
                lead.id, lead.last_message_at,
            )
            repository.reset_lead_session(session, lead)
            session_was_reset = True

        # If this is the lead's very first inbound reply and we haven't
        # tagged them as booked yet, they qualify for engagement Level 2
        # (replied to the bot). We do the actual LeadMe push AFTER the
        # transaction commits so a slow CRM call can't roll back the
        # user's message on failure.
        md_before = dict(lead.lead_metadata or {})
        already_level = md_before.get("leadme_last_level")
        # count existing USER messages after the current session start:
        reset_str = md_before.get("session_reset_at")
        from datetime import datetime as _dt
        reset_dt = None
        if reset_str:
            try:
                reset_dt = _dt.fromisoformat(reset_str)
            except ValueError:
                pass
        prior_user_msgs = sum(
            1 for m in (lead.messages or [])
            if m.role == MessageRole.user
            and (reset_dt is None or m.created_at > reset_dt)
        )
        # Trigger Level 2 (replied) on first user message unless we already
        # have a higher-engagement state (Level 1 = booked). NOTE: Level 3
        # (silent) SHOULD be overridden -- a lead who replies is no longer
        # silent. push_engagement_level enforces the upgrade rules.
        if (
            prior_user_msgs == 0
            and lead.funnel_stage != FunnelStage.handed_off
            and already_level != 1
        ):
            push_level_2 = True

        repository.add_message(session, lead, MessageRole.user, text)
        lead_id = lead.id

    # After reset, send a fresh opener so the lead gets a proper re-greeting,
    # then bail out — the agent should not also reply to the same message.
    if session_was_reset:
        try:
            from app.webhook.opener import _render_opener, _pick_topic, _greenapi_client, _chat_id
            with session_scope() as s_op:
                l_op = s_op.get(Lead, lead_id)
                if l_op is not None:
                    meta = dict(l_op.lead_metadata or {})
                    topic = _pick_topic(meta.get("opener_campaign_id"), meta)
                    opener_text = _render_opener(l_op.name, topic)
                    try:
                        _greenapi_client().sending.sendMessage(_chat_id(phone), opener_text)
                    except Exception:
                        logger.exception("[session-reset] failed to send re-opener to {}", phone)
                        opener_text = None
                    if opener_text:
                        repository.add_message(s_op, l_op, MessageRole.assistant, opener_text)
                        from datetime import datetime, timezone
                        repository.update_lead_metadata(
                            s_op, l_op,
                            opener_sent_at=datetime.now(timezone.utc).isoformat(),
                        )
                        logger.info("[session-reset] re-opener sent to {}", phone)
        except Exception:
            logger.exception("[session-reset] re-opener flow failed for {}", phone)
        return ""

    # Fire-and-forget level-2 push. Uses its own transaction so a CRM
    # failure never blocks the user-facing reply.
    if push_level_2:
        try:
            from app.crm.client import mark_engaged_no_book
            with session_scope() as s2:
                l2 = s2.get(Lead, lead_id)
                if l2 is not None:
                    mark_engaged_no_book(l2, note="first user reply")
        except Exception:
            logger.exception("[level-2] push failed for lead {}", lead_id)
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
        reply = _strip_markdown_links(reply)

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
    slot = state.get("preferred_call_slot") or "לא צוין"
    if not state.get("preferred_call_slot"):
        return (
            "NOT_AN_ERROR: אין עדיין חלון שעות מועדף. "
            "שאל את המשתמש: '9-12, 12-15, או 15-18?'"
        )
    state["call_scheduled"] = True
    state["stage"] = "handed_off"
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
    reply = _strip_markdown_links(reply)

    history.append(HumanMessage(content=text))
    history.append(AIMessage(content=reply))

    return {"reply": reply, "state": copy.deepcopy(state)}


def clear_simulation(session_id: str) -> None:
    """Wipe the in-memory session (history + state)."""
    _sim_sessions.pop(session_id, None)
