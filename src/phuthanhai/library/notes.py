"""Note semantics: scope validation, Unicode normalization, wall-clock suggestion.

The invariant that matters most in this file: **a clock time typed by the user is
never converted into a video timestamp**. ``"Lên phà lúc 5 giờ sáng"`` becomes
``wall_clock = 05:00`` (date/timezone unknown) and stays null on ``start_ms``.
Only "at 00:00:12 of the clip" produces a time range.

Suggestion is not assertion: :func:`suggest_wall_clock` only proposes a value the
UI can offer for the user to accept. Nothing is written without that acceptance.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from phuthanhai.library.store import StoreError

_MAX_TEXT = 4000

_SCOPE_VALUES = {"project", "clip", "time_range"}
_NOTE_TYPES = {"event", "context", "location", "clock_time", "person", "other"}

_CLOCK_PATTERNS = (
    # 5 giờ sáng / 5h30 chiều / 17 giờ
    re.compile(
        r"(?<!\d)(\d{1,2})\s*(?:h|giờ|g)\s*(?:(\d{1,2})\s*(?:phút|p)?)?\s*"
        r"(sáng|chiều|tối|đêm|trưa|am|pm)?",
        re.IGNORECASE,
    ),
    # 05:00, 17:30 sáng
    re.compile(
        r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*(sáng|chiều|tối|đêm|trưa|am|pm)?",
        re.IGNORECASE,
    ),
)

_PERIODS = {
    "sáng": "am",
    "am": "am",
    "chiều": "pm",
    "tối": "pm",
    "pm": "pm",
    "trưa": "noon",
    "đêm": "night",
}


def normalize_text(text: str) -> str:
    """NFC-normalize and trim; reject empty notes (an empty note is not a note)."""

    normalized = unicodedata.normalize("NFC", text or "").strip()
    if not normalized:
        raise StoreError("ANNOTATION_EMPTY", "annotation text must not be empty")
    if len(normalized) > _MAX_TEXT:
        raise StoreError("ANNOTATION_TOO_LONG", f"annotation exceeds {_MAX_TEXT} characters")
    return normalized


def validate_scope(
    *,
    scope: str,
    asset_id: str | None,
    start_ms: int | None,
    end_ms: int | None,
    duration_ms: int | None,
) -> None:
    """Enforce the three legal annotation scopes and their span bounds."""

    if scope not in _SCOPE_VALUES:
        raise StoreError("ANNOTATION_SCOPE_INVALID", f"unknown scope: {scope}")
    if scope == "project":
        if asset_id is not None or start_ms is not None or end_ms is not None:
            raise StoreError(
                "ANNOTATION_SCOPE_INVALID", "project-scoped notes must not bind an asset or a range"
            )
        return
    if not asset_id:
        raise StoreError("ANNOTATION_SCOPE_INVALID", f"{scope} notes require an asset_id")
    if duration_ms is None:
        raise StoreError("ANNOTATION_SCOPE_INVALID", "asset duration is unknown; cannot validate a range")
    if scope == "clip":
        if start_ms is not None or end_ms is not None:
            raise StoreError("ANNOTATION_SCOPE_INVALID", "clip notes must not carry a time range")
        return
    if start_ms is None or end_ms is None:
        raise StoreError("ANNOTATION_SCOPE_INVALID", "time_range notes require start_ms and end_ms")
    if not (0 <= start_ms < end_ms <= duration_ms):
        raise StoreError(
            "ANNOTATION_SCOPE_INVALID",
            f"range [{start_ms}, {end_ms}) is outside the asset duration {duration_ms} ms",
        )


def validate_note_type(note_type: str) -> str:
    if note_type not in _NOTE_TYPES:
        raise StoreError("ANNOTATION_TYPE_INVALID", f"unknown note type: {note_type}")
    return note_type


def infer_note_type(text: str) -> str:
    lowered = text.lower()
    if suggest_wall_clock(text):
        return "clock_time"
    if any(word in lowered for word in ("ở ", "tại ", "địa điểm", "bến", "cồn", "sông")):
        return "location"
    if any(word in lowered for word in ("chú rể", "cô dâu", "anh ", "chị ", "mẹ", "ba ")):
        return "person"
    return "context"


def suggest_wall_clock(text: str) -> dict[str, Any] | None:
    """Propose ``wall_clock`` from Vietnamese clock phrasing; never assert it.

    Returns ``None`` for ambiguous input (``"10 giờ đêm"`` is still 22:00, but
    ``"10 giờ"`` without a period is left to the user).
    """

    if not text:
        return None
    for pattern in _CLOCK_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        if pattern is _CLOCK_PATTERNS[1]:
            hour = int(groups[0])
            minute = int(groups[1])
            period = groups[2]
        else:
            hour = int(groups[0])
            minute = int(groups[1]) if groups[1] else 0
            period = groups[2]
        if minute > 59 or hour > 23:
            continue
        if period:
            bucket = _PERIODS.get(period.lower())
            if bucket == "pm" and hour < 12:
                hour += 12
            elif bucket == "am" and hour == 12:
                hour = 0
            elif bucket == "noon" and hour < 11:
                hour += 12
            elif bucket == "night":
                # "10 giờ đêm" -> 22:00, "12 giờ đêm" -> 00:00, "2 giờ đêm" -> 02:00
                if hour == 12:
                    hour = 0
                elif 6 <= hour < 12:
                    hour += 12
            elif bucket is None:
                continue
        elif pattern is _CLOCK_PATTERNS[0]:
            # "5 giờ" without sáng/chiều is ambiguous for 1..11; do not guess.
            if 1 <= hour <= 11:
                continue
        return {"time_of_day": f"{hour:02d}:{minute:02d}", "precision": "time_of_day"}
    return None
