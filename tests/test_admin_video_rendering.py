"""Admin rendering for video delivery events."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.admin.routes import _render_video_media_html


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


if __name__ == "__main__":
    unittest.main()
