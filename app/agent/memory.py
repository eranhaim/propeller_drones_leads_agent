"""Persisted conversation memory and duplicate-outbound safety checks."""

from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy.orm import Session

from app.db import repository
from app.db.models import Lead, Message, MessageRole


_MEMORY_KEY = "conversation_memory"
_MAX_QUESTIONS = 12
_MAX_TEXT_LENGTH = 240
_WORD_RE = re.compile(r"[\u0590-\u05FFa-zA-Z0-9]{2,}")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_QUESTION_START_RE = re.compile(
    r"^(?:האם|איזה|איזו|מה|מי|כמה|מתי|איפה|לאן|יש\s+לך|תרצה|תרצי|רוצה|רוצים)\b"
)
_STOP_WORDS = {
    "את",
    "אתה",
    "אתם",
    "אני",
    "אנחנו",
    "הוא",
    "היא",
    "הם",
    "הן",
    "זה",
    "זאת",
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
    "אם",
    "כי",
    "כדי",
    "כבר",
    "אז",
    "רק",
    "היי",
    "שלום",
}
_TOPIC_PATTERNS = (
    ("רישוי", ("רישיון", "רישוי", "רתא", "תיאוריה", "מעשי")),
    ("מחיר", ("כמה עולה", "מחיר", "עלות", "תשלום")),
    ("הכשרה", ("קורס", "לימוד", "הכשרה", "סילבוס", "לומדים")),
    ("קריירה", ("עבודה", "קריירה", "להתפרנס", "משכורת", "שכר")),
    ("תעשיות", ("מיפוי", "חקלאות", "אבטחה", "צילום", "סולארי", "שטיפה")),
    ("עתיד התחום", ("עתיד", "אוטונומי", "אוטונומיה", "בינה מלאכותית", "ai")),
)
_QUESTION_CONCEPTS = (
    ("experience", ("ניסיון", "מתחיל", "חדשה", "חדש", "מכיר")),
    ("drone", ("רחפנ", "הטס", "טס")),
    ("interest", ("מעניין", "מסקרן", "כיוון", "תחום")),
    ("call", ("שיחה", "יועץ", "להתקשר")),
)


def _compact(text: str) -> str:
    return " ".join((text or "").strip().split())


def _tokens(text: str) -> set[str]:
    tokens = set()
    for raw_word in _WORD_RE.findall(_compact(text)):
        word = raw_word.casefold()
        if word in _STOP_WORDS:
            continue
        tokens.add(word)
        if len(word) >= 5 and word[0] in "ובכלמשה":
            tokens.add(word[1:])
    return tokens


def _is_question(text: str) -> bool:
    compact = _compact(text)
    return compact.endswith("?") or bool(_QUESTION_START_RE.match(compact))


def _question_parts(text: str) -> list[str]:
    parts = [_compact(part) for part in _SENTENCE_RE.split(text) if _compact(part)]
    return [part for part in parts if _is_question(part)]


def _is_similar(left: str, right: str) -> bool:
    left_compact = _compact(left).casefold()
    right_compact = _compact(right).casefold()
    if not left_compact or not right_compact:
        return False
    if left_compact == right_compact:
        return True
    if min(len(left_compact), len(right_compact)) >= 24 and (
        left_compact in right_compact or right_compact in left_compact
    ):
        return True

    left_tokens = _tokens(left_compact)
    right_tokens = _tokens(right_compact)
    if len(left_tokens) < 3 or len(right_tokens) < 3:
        return False
    overlap = len(left_tokens & right_tokens)
    if overlap / min(len(left_tokens), len(right_tokens)) >= 0.75:
        return True
    if not (_is_question(left) and _is_question(right)):
        return False
    left_concepts = {
        name
        for name, cues in _QUESTION_CONCEPTS
        if any(cue in left_compact for cue in cues)
    }
    right_concepts = {
        name
        for name, cues in _QUESTION_CONCEPTS
        if any(cue in right_compact for cue in cues)
    }
    return len(left_concepts & right_concepts) >= 2


def _looks_like_opener(text: str) -> bool:
    normalized = _compact(text).casefold()
    return (
        "אני אלעד" in normalized
        and "פרופלור" in normalized
        and ("ניסיון" in normalized or "חדש לגמרי" in normalized)
    )


def _infer_topic(text: str) -> str | None:
    normalized = _compact(text).casefold()
    for topic, phrases in _TOPIC_PATTERNS:
        if any(phrase in normalized for phrase in phrases):
            return topic
    return None


def _memory(lead: Lead) -> dict:
    current = (lead.lead_metadata or {}).get(_MEMORY_KEY)
    return dict(current) if isinstance(current, dict) else {}


def _answered_fields(lead: Lead) -> list[str]:
    metadata = lead.lead_metadata or {}
    fields = []
    for key in ("intent", "industry", "has_experience", "preferred_call_slot"):
        if metadata.get(key) not in (None, "", "unknown", "none"):
            fields.append(key)
    return fields


def record_user_turn(session: Session, lead: Lead, text: str) -> None:
    """Persist the latest lead input as structured session memory."""
    memory = _memory(lead)
    compact = _compact(text)
    if not compact:
        return
    memory["last_user_message"] = compact[:_MAX_TEXT_LENGTH]
    topic = _infer_topic(compact)
    if topic:
        memory["current_topic"] = topic
    memory["answers_collected"] = _answered_fields(lead)
    repository.update_lead_metadata(session, lead, **{_MEMORY_KEY: memory})


def record_video_delivery(session: Session, lead: Lead, video_id: str) -> None:
    """Persist the last delivered media topic after GreenAPI accepted it."""
    memory = _memory(lead)
    memory["last_video_id"] = video_id
    memory["answers_collected"] = _answered_fields(lead)
    repository.update_lead_metadata(session, lead, **{_MEMORY_KEY: memory})


def record_assistant_turn(session: Session, lead: Lead, reply: str) -> None:
    """Record questions/openers the bot has actually sent to the lead."""
    memory = _memory(lead)
    questions = list(memory.get("asked_questions") or [])
    for question in _question_parts(reply):
        if any(_is_similar(question, item) for item in questions):
            continue
        questions.append(question[:_MAX_TEXT_LENGTH])
    memory["asked_questions"] = questions[-_MAX_QUESTIONS:]
    memory["last_assistant_message"] = _compact(reply)[:_MAX_TEXT_LENGTH]
    memory["answers_collected"] = _answered_fields(lead)
    if _looks_like_opener(reply):
        memory["opener_sent"] = True
    repository.update_lead_metadata(session, lead, **{_MEMORY_KEY: memory})


def _prior_assistant_text(messages: Iterable[Message]) -> list[str]:
    return [
        _compact(message.content)
        for message in messages
        if message.role == MessageRole.assistant and _compact(message.content)
    ]


def remove_repeated_outbound_content(
    reply: str,
    messages: Iterable[Message],
) -> tuple[str, bool]:
    """Drop an opener or question that repeats this session's prior outbound text."""
    compact_reply = _compact(reply)
    if not compact_reply:
        return reply, False

    prior = _prior_assistant_text(messages)
    if any(_is_similar(compact_reply, sent) for sent in prior):
        return "", True

    parts = [_compact(part) for part in _SENTENCE_RE.split(reply) if _compact(part)]
    kept: list[str] = []
    removed = False
    prior_questions = [
        question
        for sent in prior
        for question in _question_parts(sent)
    ]
    prior_has_opener = any(_looks_like_opener(sent) for sent in prior)
    for part in parts:
        is_repeated_question = _is_question(part) and any(
            _is_similar(part, question) for question in prior_questions
        )
        is_repeated_opener = prior_has_opener and _looks_like_opener(part)
        if is_repeated_question or is_repeated_opener:
            removed = True
            continue
        kept.append(part)
    return " ".join(kept), removed


def non_redundant_continuation(lead: Lead, messages: Iterable[Message]) -> str:
    """Return a natural fallback only when duplicate filtering emptied a text turn."""
    prior_questions = [
        question
        for sent in _prior_assistant_text(messages)
        for question in _question_parts(sent)
    ]
    metadata = lead.lead_metadata or {}
    candidates = []
    if metadata.get("has_experience") is None:
        candidates.append("יש לך כבר ניסיון עם רחפנים, או שזה תחום חדש עבורך?")
    if not metadata.get("industry"):
        candidates.append("איזה כיוון בעולם הרחפנים הכי מסקרן אותך כרגע?")
    candidates.append("מה הכי חשוב לך להבין לפני שממשיכים?")

    for candidate in candidates:
        if not any(_is_similar(candidate, prior) for prior in prior_questions):
            return candidate
    return "אפשר להמשיך בדיוק מהנקודה שהכי מעניינת אותך."


def describe_conversation_state(lead: Lead, messages: Iterable[Message]) -> str:
    """Render persisted conversation memory as explicit instructions for the agent."""
    memory = _memory(lead)
    messages_list = list(messages)
    prior_questions = [
        question
        for message in messages_list
        if message.role == MessageRole.assistant
        for question in _question_parts(message.content)
    ]
    asked_questions = list(memory.get("asked_questions") or []) + prior_questions
    unique_questions: list[str] = []
    for question in asked_questions:
        if question and not any(_is_similar(question, previous) for previous in unique_questions):
            unique_questions.append(question)

    metadata = lead.lead_metadata or {}
    known = []
    for key, label in (
        ("intent", "כוונה"),
        ("industry", "תחום"),
        ("has_experience", "ניסיון"),
        ("preferred_call_slot", "חלון שעות"),
    ):
        value = metadata.get(key)
        if value not in (None, "", "unknown", "none"):
            known.append(f"{label}={value}")

    latest_user = memory.get("last_user_message")
    if not latest_user:
        for message in reversed(messages_list):
            if message.role == MessageRole.user:
                latest_user = _compact(message.content)[:_MAX_TEXT_LENGTH]
                break

    lines = [
        "זיכרון שיחה מחייב:",
        "- זו אותה שיחה פעילה; אל תפתח שוב ב'היי, אני אלעד' ואל תחזור לשאלת גילוי שכבר נשאלה.",
        f"- מידע שנאסף: {', '.join(known) if known else 'עדיין לא נאסף מידע מובנה.'}",
        f"- נושא/התנגדות אחרונים: {memory.get('current_topic', 'לא זוהה נושא קבוע עדיין')}.",
        f"- הודעת הליד האחרונה: {latest_user or 'אין עדיין הודעת ליד שמורה.'}",
        f"- סרטון אחרון שנשלח: {memory.get('last_video_id') or 'לא נשלח סרטון עדיין.'}",
    ]
    if unique_questions:
        lines.append(
            "- שאלות שכבר נשאלו (אסור לחזור עליהן או על ניסוח מקביל): "
            + " | ".join(unique_questions[-5:])
        )
    lines.append(
        "- בחר רק תשובה או שאלה הבאה שמקדמת את השיחה ואינה חוזרת על מידע שכבר נמסר."
    )
    return "\n".join(lines)
