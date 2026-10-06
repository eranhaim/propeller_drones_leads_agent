"""Deterministic end-to-end checks for conversation memory and video ordering.

This runs the real ``handle_message`` transactions, classifier, tools, video
queue, and post-processing against synthetic ``9997…`` leads. The model is
replaced with a scripted tool caller so the assertions are offline and stable.

Run inside the bot container:

    docker compose exec -T bot python -m scripts.eval_conversation_rules
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable
from unittest.mock import Mock, patch

os.environ["LEADME_TEST_MODE"] = "1"

from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.agent.graph import AgentTurn, _history_as_messages, handle_message
from app.agent.memory import record_assistant_turn
from app.agent.tools import classify_lead, send_video
from app.db import repository
from app.db.models import Lead, MessageRole
from app.db.session import session_scope
from app.webhook.opener import OPENER_VIDEO_ID, handle_new_lead


TEST_PREFIX = "9997"
OPENER = (
    "היי ערן! אני אלעד מפרופלור דרונס. אשמח לשלוח לך סרטון שייתן לך "
    "טעימה מעולם הרחפנים. יש לך כבר ניסיון עם רחפנים, או שזה חדש לגמרי עבורך?"
)


class ScriptedAgent:
    def __init__(self, reply: str, actions: tuple[Callable[[], None], ...] = ()) -> None:
        self.reply = reply
        self.actions = actions
        self.system_prompts: list[str] = []
        self.message_contents: list[list[str]] = []

    def invoke(self, values: dict) -> dict:
        messages = values["messages"]
        self.system_prompts.append(str(messages[0].content))
        self.message_contents.append([str(message.content) for message in messages])
        for action in self.actions:
            action()
        return {"messages": [AIMessage(content=self.reply)]}


def _phone() -> str:
    return f"{TEST_PREFIX}{uuid.uuid4().int % 10**8:08d}"


def _run(agent: ScriptedAgent, phone: str, text: str) -> AgentTurn:
    with patch("app.agent.graph._agent", return_value=agent):
        result = handle_message(
            phone=phone,
            text=text,
            sender_name="TEST-CONVERSATION",
            send_video_fn=Mock(),
            defer_video_delivery=True,
        )
    assert isinstance(result, AgentTurn), type(result)
    return result


def _lead_metadata(phone: str) -> dict:
    with session_scope() as session:
        lead = session.execute(select(Lead).where(Lead.phone == phone)).scalar_one()
        return dict(lead.lead_metadata or {})


def _history_contains(phone: str, expected: str) -> bool:
    with session_scope() as session:
        lead = session.execute(select(Lead).where(Lead.phone == phone)).scalar_one()
        return any(
            expected in str(message.content)
            for message in _history_as_messages(lead, session)
        )


def _seed_opener(phone: str, *, last_user_at: datetime | None = None) -> None:
    with session_scope() as session:
        lead = repository.get_or_create_lead(session, phone=phone, name="TEST-CONVERSATION")
        repository.add_message(session, lead, MessageRole.assistant, OPENER)
        record_assistant_turn(session, lead, OPENER)
        if last_user_at is not None:
            message = repository.add_message(session, lead, MessageRole.user, "היי")
            message.created_at = last_user_at
            session.flush()


def _assert(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def _screenshot_regression() -> None:
    phone = _phone()
    _seed_opener(phone)
    agent = ScriptedAgent(
        OPENER,
        actions=(
            lambda: send_video.invoke({"video_id": "drone_academy_overview"}),
        ),
    )
    turn = _run(agent, phone, "כן, שלח")
    _assert("אני אלעד" not in turn.reply, "repeated opener was not removed")
    _assert(bool(turn.reply.strip()), "lead received a bare video with no words")
    _assert(
        [delivery.video_id for delivery in turn.video_sends] == ["drone_academy_overview"],
        "the relevant video was not queued exactly once",
    )


def _paraphrased_question_regression() -> None:
    phone = _phone()
    _seed_opener(phone)
    turn = _run(
        ScriptedAgent("יש לך ניסיון בהטסת רחפנים או שאתה מתחיל לגמרי?"),
        phone,
        "כן",
    )
    _assert("ניסיון" not in turn.reply, "paraphrased repeated question was sent")
    _assert(bool(turn.reply), "duplicate fallback must be natural text, not empty")


def _structured_state_survives_tool_invocation() -> None:
    phone = _phone()
    first = ScriptedAgent(
        "מעולה, באיזה סוג שימוש בסולארי אתה מתעניין?",
        actions=(
            lambda: classify_lead.invoke({"intent": "course", "industry": "solar"}),
        ),
    )
    _run(first, phone, "אני עובד בסולארי ורוצה להיכנס לתחום הרחפנים")
    second = ScriptedAgent("יש לי עוד פרט רלוונטי על התחום.")
    _run(second, phone, "מעניין")
    metadata = _lead_metadata(phone)
    _assert(metadata.get("industry") == "solar", "industry was lost")
    _assert(
        "תעשייה/תחום עניין: solar" in second.system_prompts[-1],
        "next turn did not receive structured lead state",
    )
    _assert(
        _history_contains(phone, "אני עובד בסולארי"),
        "next turn did not receive persisted conversation history",
    )


def _session_reset_boundaries() -> None:
    recent_phone = _phone()
    _seed_opener(recent_phone, last_user_at=datetime.now(timezone.utc) - timedelta(days=6))
    _run(ScriptedAgent("נמשיך מאיפה שעצרנו."), recent_phone, "חזרתי")
    recent = _lead_metadata(recent_phone)
    _assert(
        "session_reset_at" not in recent,
        "active conversation reset before seven idle days",
    )

    stale_phone = _phone()
    _seed_opener(stale_phone, last_user_at=datetime.now(timezone.utc) - timedelta(days=8))
    _run(ScriptedAgent("אפשר להתחיל מחדש בצורה מסודרת."), stale_phone, "חזרתי")
    stale = _lead_metadata(stale_phone)
    _assert(
        bool(stale.get("session_reset_at")),
        "long idle conversation did not record an explicit reset",
    )


def _single_video_per_turn() -> None:
    phone = _phone()
    agent = ScriptedAgent(
        "הנה ההסבר שביקשת.",
        actions=(
            lambda: send_video.invoke({"video_id": "drone_license_guide"}),
            lambda: send_video.invoke({"video_id": "drone_academy_overview"}),
        ),
    )
    turn = _run(agent, phone, "איזה רישיון מתאים לי?")
    _assert(turn.reply == "הנה ההסבר שביקשת.", "text answer changed unexpectedly")
    _assert(len(turn.video_sends) == 1, "same video was queued twice in one turn")


def _run_opener(phone: str, api: Mock) -> None:
    with patch("app.webhook.opener._greenapi_client", return_value=api):
        handle_new_lead(
            phone=phone,
            name="TEST-CONVERSATION ערן",
            metadata={"leadme_source": "אתר הבית"},
            campaign_id="12293",
        )


def _lead_videos_sent(phone: str) -> list[str]:
    with session_scope() as session:
        lead = session.execute(select(Lead).where(Lead.phone == phone)).scalar_one()
        return list(lead.videos_sent or [])


def _opener_sends_welcome_text_then_intro_video() -> None:
    phone = _phone()
    api = Mock()
    _run_opener(phone, api)

    sent = [call[0] for call in api.sending.mock_calls]
    _assert(
        sent == ["sendMessage", "sendFileByUrl"],
        f"opener must send text before media, got {sent}",
    )
    _assert(
        "אני אלעד" in api.sending.sendMessage.call_args.args[1],
        "the welcome text was not the first outbound message",
    )
    _assert(
        api.sending.sendFileByUrl.call_args.args[2] == f"{OPENER_VIDEO_ID}.mp4",
        "the opener did not send the company intro video",
    )
    _assert(
        _lead_videos_sent(phone) == [OPENER_VIDEO_ID],
        "the intro video was not marked as sent",
    )
    _assert(
        bool(_lead_metadata(phone).get("opener_sent_at")),
        "the opener was not recorded as sent",
    )


def _opener_video_failure_never_blocks_the_welcome() -> None:
    phone = _phone()
    api = Mock()
    api.sending.sendFileByUrl.side_effect = RuntimeError("GreenAPI media 500")
    _run_opener(phone, api)

    _assert(
        _history_contains(phone, "אני אלעד"),
        "a failed intro video lost the welcome text",
    )
    _assert(
        bool(_lead_metadata(phone).get("opener_sent_at")),
        "a failed intro video blocked the opener bookkeeping",
    )
    _assert(
        _lead_videos_sent(phone) == [],
        "a video that never reached GreenAPI was marked as sent",
    )


def _intro_video_is_not_offered_again_after_the_opener() -> None:
    phone = _phone()
    _run_opener(phone, Mock())
    agent = ScriptedAgent(
        "פרופלור דרונס עוסקת גם בשירותי רחפן וגם בהכשרה. מה מעניין אותך יותר?",
        actions=(
            lambda: send_video.invoke({"video_id": OPENER_VIDEO_ID}),
        ),
    )
    turn = _run(agent, phone, "מי אתם בכלל ומה החברה עושה?")

    _assert(turn.video_sends == (), "the intro video was sent to the lead twice")
    _assert(
        _lead_videos_sent(phone) == [OPENER_VIDEO_ID],
        "the intro video was recorded twice",
    )
    _assert(bool(turn.reply.strip()), "the question was left without an answer")


def _no_duplicate_video_across_a_conversation() -> None:
    phone = _phone()
    first = ScriptedAgent(
        "לרישיון עד 25 ק\"ג נדרש מבחן תיאוריה של רת\"א. מה הכיוון שמעניין אותך?",
        actions=(lambda: send_video.invoke({"video_id": "drone_license_guide"}),),
    )
    first_turn = _run(first, phone, "איזה רישיון רחפן מתאים לי?")
    _assert(
        [delivery.video_id for delivery in first_turn.video_sends]
        == ["drone_license_guide"],
        "the matching video was not sent on the first ask",
    )

    second = ScriptedAgent(
        "ההבדל הוא שהמסלול הכבד כולל גם חלק מעשי. יש לך ניסיון בהטסה?",
        actions=(lambda: send_video.invoke({"video_id": "drone_license_guide"}),),
    )
    second_turn = _run(second, phone, "ומה ההבדל בין עד 25 לכבד?")

    _assert(
        second_turn.video_sends == (),
        "the same video was sent twice in one conversation",
    )
    _assert(
        _lead_videos_sent(phone) == ["drone_license_guide"],
        "videos_sent holds a duplicate entry",
    )
    _assert(
        bool(second_turn.reply.strip()),
        "the follow-up question was left without an answer",
    )


def _video_never_replaces_the_text_answer() -> None:
    phone = _phone()
    _seed_opener(phone)
    # The LLM wrote nothing but a restatement of the caption, which the
    # duplicate filter strips. The lead must still get words.
    agent = ScriptedAgent(
        "הנה סרטון קצר על האקדמיה, ההכשרות והליווי בתהליך.",
        actions=(lambda: send_video.invoke({"video_id": "drone_academy_overview"}),),
    )
    turn = _run(agent, phone, "איך האקדמיה עובדת?")

    _assert(len(turn.video_sends) == 1, "the matching video was not sent")
    _assert(
        bool(turn.reply.strip()),
        "the lead received a bare video with no words",
    )


def _cleanup() -> None:
    with session_scope() as session:
        for lead in session.execute(
            select(Lead).where(Lead.phone.like(f"{TEST_PREFIX}%"))
        ).scalars():
            session.delete(lead)


def main() -> int:
    checks = (
        ("screenshot opener/video regression", _screenshot_regression),
        ("paraphrased duplicate question", _paraphrased_question_regression),
        ("structured state after tool", _structured_state_survives_tool_invocation),
        ("session reset boundaries", _session_reset_boundaries),
        ("single video per turn", _single_video_per_turn),
        ("opener sends text then intro video", _opener_sends_welcome_text_then_intro_video),
        ("intro video failure keeps welcome", _opener_video_failure_never_blocks_the_welcome),
        ("intro video not resent later", _intro_video_is_not_offered_again_after_the_opener),
        ("no duplicate video in a conversation", _no_duplicate_video_across_a_conversation),
        ("video never replaces the answer", _video_never_replaces_the_text_answer),
    )
    passed = 0
    try:
        for name, check in checks:
            check()
            passed += 1
            print(f"PASS {name}")
    finally:
        _cleanup()
    print(f"RESULTS: {passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
