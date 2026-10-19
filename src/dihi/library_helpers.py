"""Presentation helpers shared by the library API and tests."""
from __future__ import annotations

import re
from datetime import datetime


_OFFICIAL_SUFFIX = re.compile(
    r"\s*\((?:official\s+)?(?:music\s+)?video\)\s*$|"
    r"\s*\[(?:official\s+)?(?:music\s+)?video\]\s*$",
    re.IGNORECASE,
)


def clean_display_title(title: str | None, artist: str | None = None) -> str:
    """Return a concise browsing title while preserving the original elsewhere."""
    value = str(title or "").strip()
    artist_value = str(artist or "").strip()
    if artist_value:
        prefix = re.compile(rf"^{re.escape(artist_value)}\s*[-–—:]\s*", re.IGNORECASE)
        value = prefix.sub("", value, count=1)
    value = _OFFICIAL_SUFFIX.sub("", value).strip()
    return value or str(title or "").strip()


def human_date(value: str | None) -> str:
    """Format yt-dlp's YYYYMMDD upload date for people."""
    raw = str(value or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return raw
    try:
        date = datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        return raw
    return f"{date.strftime('%b')} {date.day}, {date.year}"
