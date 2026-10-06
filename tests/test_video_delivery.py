"""Regression coverage for direct video delivery and reply de-duplication."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.agent.context import AgentContext, VideoSend, use_context
from app.agent.graph import _dispatch_pending_videos, _remove_video_delivery_duplicates
from app.agent.tools import send_video
from app.db.models import Lead, MessageRole
from app.videos.catalog import Video


class VideoDeliveryTests(unittest.TestCase):
    def test_video_tool_queues_then_dispatches_once(self) -> None:
        video = Video(
            id="drone_license_guide",
            title="איזה רישיון רחפן מתאים לך",
            description="",
            url="https://example.test/license-guide.mp4",
            kind="file",
            trigger_stage="any",
            trigger_topics=[],
            familiarity_levels=[],
        )
        lead = Lead(phone="9990000000", videos_sent=[], lead_metadata={})
        lead.id = 1
        session = Mock()
        sent: list[tuple[Video, str]] = []

        with (
            patch("app.agent.tools.get_video", return_value=video),
            patch("app.videos.catalog.get_video", return_value=video),
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
                        "caption": "סרטון קצר שעושה סדר ברישיונות",
                    }
                )
            self.assertEqual(sent, [])
            _dispatch_pending_videos(session, lead, context)

        self.assertEqual(sent, [(video, "סרטון קצר שעושה סדר ברישיונות")])
        add_message.assert_called_once()
        _, _, role, content = add_message.call_args.args
        metadata = add_message.call_args.kwargs["metadata"]
        self.assertEqual(role, MessageRole.system)
        self.assertIn("נשלח סרטון: איזה רישיון רחפן מתאים לך", content)
        self.assertEqual(
            metadata,
            {
                "event": "video_sent",
                "video_id": "drone_license_guide",
                "video_title": "איזה רישיון רחפן מתאים לך",
                "caption": "סרטון קצר שעושה סדר ברישיונות",
                "video_kind": "file",
            },
        )
        self.assertEqual(
            context.video_sends_this_turn,
            [
                VideoSend(
                    video_id="drone_license_guide",
                    title="איזה רישיון רחפן מתאים לך",
                    caption="סרטון קצר שעושה סדר ברישיונות",
                )
            ],
        )
        self.assertIn("הוכן למשלוח אחרי התשובה", result)

    def test_duplicate_video_caption_is_removed_but_new_follow_up_remains(self) -> None:
        sends = [
            VideoSend(
                video_id="drone_license_guide",
                title="איזה רישיון רחפן מתאים לך",
                caption=(
                    "שלחתי לך סרטון שעושה סדר בסוגי הרישיונות. "
                    "צפה בו כשנוח לך."
                ),
            )
        ]

        reply = (
            "שלחתי לך סרטון שעושה סדר בסוגי הרישיונות. "
            "צפה בו כשנוח לך. "
            "לרישיון עד 25 ק״ג נדרש מבחן תיאוריה של רת״א בלבד."
        )

        self.assertEqual(
            _remove_video_delivery_duplicates(reply, sends),
            "לרישיון עד 25 ק״ג נדרש מבחן תיאוריה של רת״א בלבד.",
        )

    def test_second_different_video_is_blocked_in_the_same_turn(self) -> None:
        first = Video(
            id="drone_license_guide",
            title="רישיונות",
            description="",
            url="https://example.test/license.mp4",
            kind="file",
            trigger_stage="any",
            trigger_topics=[],
            familiarity_levels=[],
        )
        second = Video(
            id="drone_academy_overview",
            title="אקדמיה",
            description="",
            url="https://example.test/academy.mp4",
            kind="file",
            trigger_stage="any",
            trigger_topics=[],
            familiarity_levels=[],
        )
        lead = Lead(phone="9990000000", videos_sent=[], lead_metadata={})
        lead.id = 1
        context = AgentContext(session=Mock(), lead=lead, send_video=Mock())

        with patch(
            "app.agent.tools.get_video",
            side_effect=[first, second],
        ):
            with use_context(context):
                send_video.invoke({"video_id": first.id})
                result = send_video.invoke({"video_id": second.id})

        self.assertIn("סרטון אחד", result)
        self.assertEqual(
            [delivery.video_id for delivery in context.video_sends_this_turn],
            [first.id],
        )


if __name__ == "__main__":
    unittest.main()
