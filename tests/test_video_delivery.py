"""Regression coverage for direct video delivery and reply de-duplication."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.agent.context import AgentContext, VideoSend, use_context
from app.agent.graph import (
    _dispatch_pending_videos,
    _enforce_intro_video,
    _limit_questions,
    _remove_video_delivery_duplicates,
    _sim_send_video,
    _sim_state_var,
    _sim_turn_videos_var,
)
from app.agent.tools import send_video
from app.db.models import Lead, Message, MessageRole
from app.videos.catalog import INTRO_VIDEO_ID, Video


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
            patch("app.agent.graph.get_video", return_value=video),
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

    def test_extra_question_is_removed_without_dropping_the_answer(self) -> None:
        reply = (
            "המחיר תלוי במסלול. מה הכי מעניין אותך? "
            "יש לך כבר ניסיון עם רחפנים? המחיר המדויק יינתן על ידי יועץ."
        )

        self.assertEqual(
            _limit_questions(reply),
            "המחיר תלוי במסלול. מה הכי מעניין אותך? המחיר המדויק יינתן על ידי יועץ.",
        )


class IntroVideoTests(unittest.TestCase):
    """Every conversation opens with the company intro video."""

    def _context(self, lead: Lead) -> AgentContext:
        return AgentContext(session=Mock(), lead=lead, send_video=Mock())

    def _lead(self, videos_sent=None) -> Lead:
        lead = Lead(phone="9990000001", videos_sent=videos_sent or [], lead_metadata={})
        lead.id = 1
        return lead

    def _assistant_message(self) -> Message:
        message = Message()
        message.role = MessageRole.assistant
        message.content = "שלום, אני אלעד"
        return message

    def test_first_turn_queues_the_intro(self) -> None:
        lead = self._lead()
        context = self._context(lead)

        _enforce_intro_video(lead, context, [])

        self.assertEqual(
            [send.video_id for send in context.video_sends_this_turn],
            [INTRO_VIDEO_ID],
        )

    def test_an_already_sent_intro_is_not_repeated(self) -> None:
        lead = self._lead(videos_sent=[INTRO_VIDEO_ID])
        context = self._context(lead)

        _enforce_intro_video(lead, context, [])

        self.assertEqual(context.video_sends_this_turn, [])

    def test_later_turns_do_not_queue_the_intro(self) -> None:
        lead = self._lead()
        context = self._context(lead)

        _enforce_intro_video(lead, context, [self._assistant_message()])

        self.assertEqual(context.video_sends_this_turn, [])

    def test_a_video_the_agent_chose_itself_wins(self) -> None:
        lead = self._lead()
        context = self._context(lead)
        context.video_sends_this_turn.append(
            VideoSend(video_id="drone_license_guide", title="רישיונות", caption="")
        )

        _enforce_intro_video(lead, context, [])

        self.assertEqual(
            [send.video_id for send in context.video_sends_this_turn],
            ["drone_license_guide"],
        )


class SimulatorVideoEventTests(unittest.TestCase):
    """The simulator has to report what it 'sent' so the admin can play it."""

    def _run_stub_tool(self, video: Video, **kwargs) -> list[dict]:
        state = {"videos_sent": []}
        turn_videos: list[dict] = []
        state_token = _sim_state_var.set(state)
        videos_token = _sim_turn_videos_var.set(turn_videos)
        try:
            with patch("app.videos.catalog.get_video", return_value=video):
                _sim_send_video.invoke({"video_id": video.id, **kwargs})
        finally:
            _sim_state_var.reset(state_token)
            _sim_turn_videos_var.reset(videos_token)
        return turn_videos

    def test_stub_tool_reports_the_video_with_its_catalog_caption(self) -> None:
        video = Video(
            id="drone_license_guide",
            title="איזה רישיון רחפן מתאים לך",
            description="",
            caption="סרטון על רישיונות",
            url="https://example.test/license-guide.mp4",
            kind="file",
            trigger_stage="any",
            trigger_topics=[],
            familiarity_levels=[],
        )

        turn_videos = self._run_stub_tool(video)

        self.assertEqual(turn_videos, [{
            "video_id": "drone_license_guide",
            "title": "איזה רישיון רחפן מתאים לך",
            "caption": "סרטון על רישיונות",
        }])

    def test_an_explicit_caption_wins_over_the_catalog_caption(self) -> None:
        video = Video(
            id="drone_license_guide",
            title="איזה רישיון רחפן מתאים לך",
            description="",
            caption="סרטון על רישיונות",
            url="https://example.test/license-guide.mp4",
            kind="file",
            trigger_stage="any",
            trigger_topics=[],
            familiarity_levels=[],
        )

        turn_videos = self._run_stub_tool(video, caption="כיתוב מהבוט")

        self.assertEqual(turn_videos[0]["caption"], "כיתוב מהבוט")


if __name__ == "__main__":
    unittest.main()
