"""Deterministic regressions for durable conversation memory."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.agent.memory import (
    describe_conversation_state,
    record_assistant_turn,
    record_user_turn,
    remove_repeated_outbound_content,
)
from app.db.models import Lead, Message, MessageRole


def _message(role: MessageRole, content: str) -> Message:
    message = Message()
    message.role = role
    message.content = content
    return message


class ConversationMemoryTests(unittest.TestCase):
    def _lead(self) -> Lead:
        lead = Lead(phone="9990000000", lead_metadata={}, videos_sent=[])
        lead.id = 1
        return lead

    def test_screenshot_sequence_drops_repeated_opener_after_video(self) -> None:
        opener = (
            "היי ערן! אני אלעד מפרופלור דרונס. אשמח לשלוח לך סרטון שייתן לך "
            "טעימה מעולם הרחפנים. יש לך כבר ניסיון עם רחפנים, או שזה חדש לגמרי עבורך?"
        )
        prior = [
            _message(MessageRole.assistant, opener),
            _message(MessageRole.system, "נשלח סרטון: סרטון היכרות"),
        ]

        reply, removed = remove_repeated_outbound_content(opener, prior)

        self.assertTrue(removed)
        self.assertEqual(reply, "")

    def test_paraphrased_repeated_discovery_question_is_dropped(self) -> None:
        prior = [
            _message(
                MessageRole.assistant,
                "יש לך כבר ניסיון עם רחפנים, או שזה חדש לגמרי עבורך?",
            )
        ]

        reply, removed = remove_repeated_outbound_content(
            "יש לך ניסיון בהטסת רחפנים או שאתה מתחיל לגמרי?",
            prior,
        )

        self.assertTrue(removed)
        self.assertEqual(reply, "")

    def test_new_answer_and_prior_question_are_rendered_for_next_turn(self) -> None:
        lead = self._lead()
        session = Mock()
        record_assistant_turn(
            session,
            lead,
            "יש לך כבר ניסיון עם רחפנים, או שזה חדש לגמרי עבורך?",
        )
        record_user_turn(session, lead, "כן, אני מטיס בשביל הכיף ורוצה להבין איזה רישיון צריך.")

        state = describe_conversation_state(
            lead,
            [
                _message(
                    MessageRole.assistant,
                    "יש לך כבר ניסיון עם רחפנים, או שזה חדש לגמרי עבורך?",
                ),
                _message(
                    MessageRole.user,
                    "כן, אני מטיס בשביל הכיף ורוצה להבין איזה רישיון צריך.",
                ),
            ],
        )

        self.assertIn("איזה רישיון צריך", state)
        self.assertIn("יש לך כבר ניסיון עם רחפנים", state)
        self.assertIn("רישוי", state)

    def test_new_question_after_video_is_kept(self) -> None:
        prior = [
            _message(
                MessageRole.assistant,
                "יש לך כבר ניסיון עם רחפנים, או שזה חדש לגמרי עבורך?",
            )
        ]

        reply, removed = remove_repeated_outbound_content(
            "לרישיון עד 25 ק״ג יש תיאוריה ומבחן מקוון. איזה שימוש מעניין אותך יותר?",
            prior,
        )

        self.assertFalse(removed)
        self.assertIn("איזה שימוש", reply)


if __name__ == "__main__":
    unittest.main()
