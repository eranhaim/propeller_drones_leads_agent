"""Admin rendering for video delivery events."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.admin.routes import _render_video_event_html, _render_video_media_html


def _video(kind: str, url: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="test_video",
        title="Test Video",
        description="",
        url=url,
        kind=kind,
        trigger_stage="any",
        trigger_topics=[],
        familiarity_levels=[],
    )


class AdminVideoRenderingTests(unittest.TestCase):
    @patch("app.admin.routes._get_video_from_catalog")
    def test_direct_video_file_renders_html5_player(self, get_video) -> None:
        get_video.return_value = _video("file", "https://example.test/video.mp4")

        html = _render_video_media_html("test_video")

        self.assertIn("<video", html)
        self.assertIn("controls", html)
        self.assertIn('preload="metadata"', html)
        self.assertIn('src="https://example.test/video.mp4"', html)
        self.assertNotIn("autoplay", html)

    @patch("app.admin.routes._get_video_from_catalog")
    def test_link_video_renders_clickable_url(self, get_video) -> None:
        get_video.return_value = _video("link", "https://youtu.be/example")

        html = _render_video_media_html("test_video")

        self.assertNotIn("<video", html)
        self.assertIn('href="https://youtu.be/example"', html)
        self.assertIn("https://youtu.be/example", html)
        self.assertIn("Test Video", html)

    @patch("app.admin.routes._get_video_from_catalog")
    def test_invalid_video_url_is_not_rendered_as_link(self, get_video) -> None:
        get_video.return_value = _video("file", "javascript:alert(1)")

        html = _render_video_media_html("test_video")

        self.assertNotIn("href=", html)
        self.assertNotIn("src=", html)
        self.assertNotIn("javascript:alert", html)


class VideoEventBlockTests(unittest.TestCase):
    """The block the conversation view and the simulator both render."""

    @patch("app.admin.routes._get_video_from_catalog")
    def test_block_carries_the_player_and_the_caption(self, get_video) -> None:
        get_video.return_value = _video("file", "https://example.test/video.mp4")

        html = _render_video_event_html(
            video_id="test_video",
            video_title="Test Video",
            caption="watch this",
            meta="בוט · 12:30",
        )

        self.assertIn('<div class="video-event">', html)
        self.assertIn('<video class="video-player"', html)
        self.assertIn('src="https://example.test/video.mp4"', html)
        self.assertIn("watch this", html)
        self.assertIn("בוט · 12:30", html)

    @patch("app.admin.routes._get_video_from_catalog")
    def test_block_escapes_every_caller_supplied_field(self, get_video) -> None:
        get_video.return_value = _video("file", "https://example.test/video.mp4")

        html = _render_video_event_html(
            video_id="<id>",
            video_title="<script>alert(1)</script>",
            caption="<img src=x onerror=1>",
            meta="<b>meta</b>",
        )

        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>meta</b>", html)

    @patch("app.admin.routes._get_video_from_catalog")
    def test_block_omits_the_caption_line_when_there_is_no_caption(
        self, get_video
    ) -> None:
        get_video.return_value = _video("file", "https://example.test/video.mp4")

        html = _render_video_event_html(
            video_id="test_video",
            video_title="Test Video",
            caption="",
            meta="בוט · 12:30",
        )

        self.assertNotIn("כיתוב שנשלח", html)


if __name__ == "__main__":
    unittest.main()
