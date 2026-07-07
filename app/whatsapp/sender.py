"""Outbound WhatsApp actions bound to a specific chat."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from app.videos.catalog import Video

# Human-feel typing simulation: never faster than 2s, never slower than 6s,
# roughly one second per ~35 characters (a plausible typing speed). Keeps
# the WhatsApp conversation from feeling robotic (instant replies) while
# not making users wait forever on long answers.
_TYPING_MIN_SECONDS = 2.0
_TYPING_MAX_SECONDS = 6.0
_TYPING_CHARS_PER_SECOND = 35.0


def _typing_delay_for(text: str) -> float:
    length = len(text or "")
    if length <= 0:
        return _TYPING_MIN_SECONDS
    est = length / _TYPING_CHARS_PER_SECOND
    return max(_TYPING_MIN_SECONDS, min(_TYPING_MAX_SECONDS, est))


@dataclass
class ChatSender:
    """Send text and files to a single WhatsApp chat via GreenAPI.

    ``chat_id`` is the WhatsApp id, e.g. ``972501234567@c.us``.
    ``api`` is a ``whatsapp_api_client_python.API.GreenAPI`` instance
    (typically ``notification.api`` inside a chatbot handler).
    """

    api: object
    chat_id: str

    def send_text(self, text: str, humanize: bool = True) -> None:
        if not text:
            return
        if humanize:
            # Re-fire typing indicator right before the wait, then sleep for a
            # length-proportional delay so the "typing..." bubble stays up
            # while the reply is "being typed". Feels like a person.
            self.send_typing()
            time.sleep(_typing_delay_for(text))
        try:
            self.api.sending.sendMessage(self.chat_id, text)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Failed to send text to {}", self.chat_id)
            raise

    def send_video(self, video: Video, caption: Optional[str] = None) -> None:
        """Send a piece of media from the catalog.

        For ``kind="file"`` -- fetches the URL as a native WhatsApp video.
        For ``kind="link"`` -- sends a text message with the URL so WhatsApp
        renders a link preview (used for YouTube / vimeo / etc that can't be
        streamed as native video).
        """
        try:
            if video.kind == "link":
                text_caption = caption or video.title
                message = f"{text_caption}\n{video.url}"
                self.api.sending.sendMessage(self.chat_id, message)  # type: ignore[attr-defined]
                return

            file_name = f"{video.id}.mp4"
            self.api.sending.sendFileByUrl(  # type: ignore[attr-defined]
                self.chat_id,
                video.url,
                file_name,
                caption or video.title,
            )
        except Exception:
            logger.exception(
                "Failed to send media {} (kind={}) to {}",
                video.id, video.kind, self.chat_id,
            )
            raise

    def send_typing(self) -> None:
        """Best-effort typing indicator (not all GreenAPI plans support this)."""
        try:
            if hasattr(self.api.sending, "showMessagePreview"):  # type: ignore[attr-defined]
                self.api.sending.showMessagePreview(self.chat_id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
