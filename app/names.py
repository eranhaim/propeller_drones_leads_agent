"""Deciding whether a lead's stored name is safe to greet them by.

LeadMe hands us whatever the ad platform captured, and for some leads that is
a random token rather than a name. 25 leads were greeted with messages like
"היי PAHOOCVnUOcDqBoRjZdQsYWu 🙋" before this check existed. Every canned
outbound message (opener, nudge, video follow-up) goes through
:func:`first_name` and falls back to its anonymous template when the name is
not usable.
"""

from __future__ import annotations

import re
from typing import Optional


_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_LATIN_RE = re.compile(r"^[A-Za-z'\-]+$")
_VOWEL_RE = re.compile(r"[aeiouAEIOU]")

# Real first names are short. Every junk token we saw in production was a long
# random Latin string, so length alone separates them cleanly.
_MAX_LATIN_LEN = 11
_MAX_LEN = 20


def first_name(raw: Optional[str]) -> Optional[str]:
    """Return the lead's first name, or None if it is not safe to use.

    Rejects empty values, phone numbers, and the random Latin tokens LeadMe
    sometimes stores in the name field.
    """
    candidate = (raw or "").strip().split(" ", 1)[0].strip()
    if len(candidate) < 2 or len(candidate) > _MAX_LEN:
        return None
    if not _LETTER_RE.search(candidate):
        return None
    if _LATIN_RE.match(candidate):
        if len(candidate) > _MAX_LATIN_LEN:
            return None
        # A name a human can pronounce has a vowel. "vLGAi" style tokens do not.
        if not _VOWEL_RE.search(candidate):
            return None
    return candidate
