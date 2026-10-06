"""Deterministic coverage for the replacement lead-nurture catalogue."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from app.agent.context import AgentContext
from app.agent.graph import _enforce_video_promise
from app.db.models import FamiliarityLevel, Lead
from app.videos.catalog import load_catalog, recommend
from app.whatsapp.sender import ChatSender


class VideoCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_catalog.cache_clear()

    def test_each_topic_and_indirect_phrase_selects_its_video(self) -> None:
        cases = {
            "propeller_company_overview": "אני לא מכיר אתכם בכלל, מי עומד מאחורי האקדמיה?",
            "drone_academy_overview": "איך נראית ההכשרה ומה באמת מקבלים בקורס?",
            "drone_license_guide": "אני רוצה להטיס רק בשביל הכיף, איזה אישור צריך?",
            "drone_career_paths": "אפשר לבנות מזה קריירה או שזה רק תחביב?",
            "drone_industries": "באילו עבודות באמת משתמשים ברחפנים היום?",
            "drone_industry_future": "האם AI ואוטונומיה משנים את העתיד של המקצוע?",
            "modern_drone_history": "איך הטכנולוגיה הזאת נהייתה נגישה לכל כך הרבה אנשים?",
        }

        for expected_id, context in cases.items():
            with self.subTest(expected_id=expected_id):
                video = recommend(
                    familiarity="beginner",
                    topics_context=[context],
                )
                self.assertIsNotNone(video)
                self.assertEqual(video.id, expected_id)

    def test_each_video_handles_a_second_indirect_hebrew_variant(self) -> None:
        cases = {
            "propeller_company_overview": "אתם רק בית ספר או שיש לכם עוד פעילות?",
            "drone_academy_overview": "אילו מסלולים קיימים אצלכם בהכשרה?",
            "drone_license_guide": "אני רוצה לעבוד עם רחפן, מה צריך להוציא?",
            "drone_career_paths": "אפשר לבנות מזה פרנסה אמיתית?",
            "drone_industries": "יש שימוש ברחפנים בתחום הבנייה והסולארי?",
            "drone_industry_future": "מה יקרה למטיסים כשה-AI יתפתח?",
            "modern_drone_history": "למה DJI הפכה את הרחפנים לכל כך נגישים?",
        }
        for expected_id, context in cases.items():
            with self.subTest(expected_id=expected_id):
                video = recommend(
                    familiarity="beginner",
                    topics_context=[context],
                )
                self.assertIsNotNone(video)
                self.assertEqual(video.id, expected_id)

    def test_no_match_does_not_push_a_generic_video(self) -> None:
        self.assertIsNone(
            recommend(
                familiarity="beginner",
                topics_context=["איפה החניה במתחם?"],
            )
        )

    def test_every_video_has_a_plain_hebrew_catalog_caption(self) -> None:
        for video in load_catalog():
            with self.subTest(video_id=video.id):
                self.assertTrue(video.caption)
                self.assertNotIn("[", video.caption)
                self.assertNotIn("](", video.caption)

    def test_sent_video_is_never_recommended_again(self) -> None:
        video = recommend(
            familiarity="beginner",
            topics_context=["איזה אישור צריך כדי להטיס רחפן?"],
            exclude_ids=["drone_license_guide"],
        )
        self.assertNotEqual(
            video.id if video else None,
            "drone_license_guide",
        )

    @patch("app.agent.graph.queue_video")
    def test_video_promise_safety_net_uses_the_leads_actual_topic(
        self,
        queue_video: Mock,
    ) -> None:
        lead = Lead(
            phone="9990000000",
            videos_sent=[],
            lead_metadata={},
            familiarity_level=FamiliarityLevel.beginner,
        )
        lead.id = 1
        context = AgentContext(session=Mock(), lead=lead, send_video=Mock())

        _enforce_video_promise(
            Mock(),
            lead,
            context,
            "אסביר בקצרה ואז אשלח לך סרטון שמסדר את זה.",
            "איזה רישיון צריך אם אני רוצה להטיס רק כתחביב?",
        )

        queue_video.assert_called_once()
        self.assertEqual(
            queue_video.call_args.args[1].id,
            "drone_license_guide",
        )

    @patch("app.agent.graph.queue_video")
    def test_video_promise_safety_net_refuses_an_irrelevant_send(
        self,
        queue_video: Mock,
    ) -> None:
        lead = Lead(
            phone="9990000000",
            videos_sent=[],
            lead_metadata={},
            familiarity_level=FamiliarityLevel.beginner,
        )
        lead.id = 1
        context = AgentContext(session=Mock(), lead=lead, send_video=Mock())

        _enforce_video_promise(
            Mock(),
            lead,
            context,
            "אשלח לך סרטון שיעזור.",
            "איפה החניה במתחם?",
        )

        queue_video.assert_not_called()

    def test_every_catalog_asset_uses_native_greenapi_delivery(self) -> None:
        api = Mock()
        sender = ChatSender(api=api, chat_id="972501234567@c.us")

        for video in load_catalog():
            with self.subTest(video_id=video.id):
                sender.send_video(video, "כיתוב בדיקה")
                api.sending.sendFileByUrl.assert_called_with(
                    "972501234567@c.us",
                    video.url,
                    f"{video.id}.mp4",
                    "כיתוב בדיקה",
                )

    def test_transcripts_cover_exactly_the_deliverable_catalog(self) -> None:
        entries = json.loads(
            Path("data/video_transcripts.json").read_text(encoding="utf-8")
        )
        catalog_ids = {video.id for video in load_catalog()}
        transcript_ids = {entry["id"] for entry in entries}

        self.assertEqual(transcript_ids, catalog_ids)
        for entry in entries:
            with self.subTest(video_id=entry["id"]):
                self.assertTrue(entry["source_filename"])
                self.assertGreater(entry["duration_seconds"], 0)
                self.assertGreater(len(entry["transcript"]), 100)

    @patch("app.videos.catalog._video_match_score")
    @patch("app.videos.catalog._semantic_video_ranks")
    def test_rag_rank_breaks_a_catalog_cue_tie(
        self,
        semantic_ranks: Mock,
        match_score: Mock,
    ) -> None:
        semantic_ranks.return_value = {
            "drone_industries": 1,
            "drone_career_paths": 2,
        }
        match_score.side_effect = (
            lambda video, _context: 1
            if video.id in semantic_ranks.return_value
            else 0
        )

        video = recommend(
            familiarity="beginner",
            topics_context=["יש עבודה וצילום עם רחפנים?"],
        )

        self.assertIsNotNone(video)
        self.assertEqual(video.id, "drone_industries")
        semantic_ranks.assert_called_once()


if __name__ == "__main__":
    unittest.main()
