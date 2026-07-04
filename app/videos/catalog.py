"""Video catalog: load, search, and recommend videos to send to leads."""

from __future__ import annotations

import json
from dataclasses import dataclass
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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "kind": self.kind,
            "trigger_stage": self.trigger_stage,
            "trigger_topics": list(self.trigger_topics),
            "familiarity_levels": list(self.familiarity_levels),
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
            trigger_stage=v.get("trigger_stage", "any"),
            trigger_topics=list(v.get("trigger_topics", [])),
            familiarity_levels=list(v.get("familiarity_levels", [])),
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


def recommend(
    familiarity: str,
    topics_context: Optional[Sequence[str]] = None,
    exclude_ids: Optional[Sequence[str]] = None,
) -> Optional[Video]:
    """Pick the best video for the current state.

    Preference order:
    1. Familiarity level match + topic keyword match in the recent context.
    2. Familiarity level match only.
    3. Any video that hasn't been sent yet.
    """
    exclude = set(exclude_ids or [])
    context_lc = " ".join(topics_context or []).lower()

    candidates = [v for v in load_catalog() if v.id not in exclude]
    if not candidates:
        return None

    def _familiarity_ok(v: Video) -> bool:
        return not v.familiarity_levels or familiarity in v.familiarity_levels

    def _topic_score(v: Video) -> int:
        if not context_lc:
            return 0
        return sum(1 for kw in v.trigger_topics if kw.lower() in context_lc)

    ranked = sorted(
        candidates,
        key=lambda v: (_familiarity_ok(v), _topic_score(v)),
        reverse=True,
    )
    return ranked[0] if ranked else None
