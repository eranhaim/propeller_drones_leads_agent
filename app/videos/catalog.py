"""Video catalog: load, search, and recommend videos to send to leads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence

from loguru import logger

VIDEOS_JSON = Path("data/videos.json")


@dataclass(frozen=True)
class Video:
    """A piece of media the bot can send to a lead.

    ``kind`` decides how it's delivered:
    - ``"file"`` (default): a public MP4 URL, sent as a native WhatsApp video
      via GreenAPI's ``sendFileByUrl``.
    - ``"link"``: an external URL (e.g. YouTube) that WhatsApp can't stream
      natively -- sent as a plain text message so WhatsApp renders a link
      preview with thumbnail.
    """

    id: str
    title: str
    description: str
    url: str
    trigger_stage: str
    trigger_topics: List[str]
    familiarity_levels: List[str]
    kind: str = "file"
    caption: str = ""
    matching_cues: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "kind": self.kind,
            "caption": self.caption,
            "trigger_stage": self.trigger_stage,
            "trigger_topics": list(self.trigger_topics),
            "familiarity_levels": list(self.familiarity_levels),
            "matching_cues": list(self.matching_cues),
        }


@lru_cache(maxsize=1)
def load_catalog(path: Path = VIDEOS_JSON) -> List[Video]:
    if not path.exists():
        logger.warning("Video catalog not found at {}", path)
        return []

    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    videos = [
        Video(
            id=v["id"],
            title=v["title"],
            description=v["description"],
            url=v["url"],
            kind=v.get("kind", "file"),
            caption=v.get("caption", ""),
            trigger_stage=v.get("trigger_stage", "any"),
            trigger_topics=list(v.get("trigger_topics", [])),
            familiarity_levels=list(v.get("familiarity_levels", [])),
            matching_cues=list(v.get("matching_cues", [])),
        )
        for v in raw
    ]
    logger.info("Loaded {} videos from catalog", len(videos))
    return videos


def get_video(video_id: str) -> Optional[Video]:
    for v in load_catalog():
        if v.id == video_id:
            return v
    return None


def list_for_prompt() -> str:
    """Return a compact catalog listing for injection into the LLM system prompt."""
    lines = ["Available media (call send_video with the id):"]
    for v in load_catalog():
        topics = ", ".join(v.trigger_topics) if v.trigger_topics else "-"
        levels = ", ".join(v.familiarity_levels) if v.familiarity_levels else "any"
        kind_note = "native video" if v.kind == "file" else "link preview"
        lines.append(
            f"- id={v.id} | kind={v.kind} ({kind_note}) | "
            f"title=\"{v.title}\" | when={v.trigger_stage} "
            f"| levels=[{levels}] | topics=[{topics}]"
        )
        lines.append(f"  desc: {v.description}")
    return "\n".join(lines)


_HEBREW_WORD_RE = re.compile(r"[\u0590-\u05FF]{2,}")
_LATIN_WORD_RE = re.compile(r"[a-z]{2,}")
_MATCH_STOP_WORDS = {
    "את",
    "אתה",
    "אתם",
    "אני",
    "אני",
    "איך",
    "אין",
    "אבל",
    "הוא",
    "היא",
    "זה",
    "זאת",
    "של",
    "עם",
    "על",
    "גם",
    "כל",
    "לא",
    "לי",
    "לך",
    "לכם",
    "מה",
    "מי",
    "אם",
    "כי",
    "רוצה",
    "רוצים",
    "יכול",
    "יכולה",
}


def _words(text: str) -> set[str]:
    normalized = (text or "").lower()
    return {
        word
        for word in (
            _HEBREW_WORD_RE.findall(normalized)
            + _LATIN_WORD_RE.findall(normalized)
        )
        if word not in _MATCH_STOP_WORDS
    }


def _video_match_score(video: Video, context: str) -> int:
    context_words = _words(context)
    if not context_words:
        return 0

    score = 0
    for cue in video.matching_cues + video.trigger_topics:
        cue_words = _words(cue)
        if not cue_words:
            continue
        if len(cue_words) > 1 and cue.lower() in context.lower():
            score += 4
        score += len(context_words & cue_words)
    return score


def recommend(
    familiarity: str,
    topics_context: Optional[Sequence[str]] = None,
    exclude_ids: Optional[Sequence[str]] = None,
) -> Optional[Video]:
    """Pick an unsent video that matches the current conversation.

    A video is never returned without a semantic cue match. This prevents a
    generic unsent video from being pushed into an unrelated conversation.
    """
    exclude = set(exclude_ids or [])
    context = " ".join(topics_context or [])

    candidates = [v for v in load_catalog() if v.id not in exclude]
    if not candidates or not context.strip():
        return None

    def _familiarity_ok(v: Video) -> bool:
        return not v.familiarity_levels or familiarity in v.familiarity_levels

    ranked = sorted(
        candidates,
        key=lambda v: (_video_match_score(v, context), _familiarity_ok(v)),
        reverse=True,
    )
    best = ranked[0] if ranked else None
    return best if best and _video_match_score(best, context) else None
