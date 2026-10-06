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

from app.agent.graph import AgentTurn, handle_message
from app.agent.memory import record_assistant_turn
from app.agent.tools import classify_lead, send_video
from app.db import repository
from app.db.models import Lead, MessageRole
from app.db.session import session_scope


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

    def invoke(self, values: dict) -> dict:
        self.system_prompts.append(str(values["messages"][0].content))
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
    _assert(turn.reply == "", "repeated opener was not removed after video queue")
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
        "אני עובד בסולארי" in second.system_prompts[-1],
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
