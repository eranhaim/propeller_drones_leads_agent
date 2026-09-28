"""Regression coverage for direct video delivery and reply de-duplication."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.agent.context import AgentContext, VideoSend, use_context
from app.agent.graph import _remove_video_delivery_duplicates
from app.agent.tools import send_video
from app.db.models import Lead, MessageRole
from app.videos.catalog import Video


class VideoDeliveryTests(unittest.TestCase):
    def test_video_tool_sends_once_and_persists_admin_event(self) -> None:
        video = Video(
            id="course_webinar_full",
            title="וובינר מלא",
            description="",
            url="https://example.test/webinar",
            kind="link",
            trigger_stage="warm",
            trigger_topics=[],
            familiarity_levels=[],
        )
        lead = Lead(phone="9990000000", videos_sent=[], lead_metadata={})
        lead.id = 1
        session = Mock()
        sent: list[tuple[Video, str]] = []

        with (
            patch("app.agent.tools.get_video", return_value=video),
            patch("app.agent.tools.repository.add_message") as add_message,
        ):
            context = AgentContext(
                session=session,
                lead=lead,
                send_video=lambda sent_video, caption: sent.append(
                    (sent_video, caption)
                ),
            )
            with use_context(context):
                result = send_video.invoke(
                    {
                        "video_id": video.id,
                        "caption": "הוובינר המלא מחכה לך",
                    }
                )

        self.assertEqual(sent, [(video, "הוובינר המלא מחכה לך")])
        add_message.assert_called_once()
        _, _, role, content = add_message.call_args.args
        metadata = add_message.call_args.kwargs["metadata"]
        self.assertEqual(role, MessageRole.system)
        self.assertIn("נשלח סרטון: וובינר מלא", content)
        self.assertEqual(
            metadata,
            {
                "event": "video_sent",
                "video_id": "course_webinar_full",
                "video_title": "וובינר מלא",
                "caption": "הוובינר המלא מחכה לך",
                "video_kind": "link",
            },
        )
        self.assertEqual(
            context.video_sends_this_turn,
            [
                VideoSend(
                    video_id="course_webinar_full",
                    title="וובינר מלא",
                    caption="הוובינר המלא מחכה לך",
                )
            ],
        )
        self.assertIn("השאר את התשובה ריקה", result)

    def test_duplicate_video_caption_is_removed_but_new_follow_up_remains(self) -> None:
        sends = [
            VideoSend(
                video_id="course_webinar_full",
                title="וובינר מלא",
                caption=(
                    "שלחתי לך את הוובינר המלא שסוקר את התחום והמסלולים. "
                    "תראה כשנוח לך ותגיד לי מה חשבת."
                ),
            )
        ]

        reply = (
            "שלחתי לך את הוובינר המלא שסוקר את התחום והמסלולים. "
            "תראה כשנוח לך ותגיד לי מה חשבת. "
            "לרישיון עד 25 ק״ג נדרש מבחן תיאוריה מקוון בלבד."
        )

        self.assertEqual(
            _remove_video_delivery_duplicates(reply, sends),
            "לרישיון עד 25 ק״ג נדרש מבחן תיאוריה מקוון בלבד.",
        )


if __name__ == "__main__":
    unittest.main()
