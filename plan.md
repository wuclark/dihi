# Plan: Embed Lyrics into Audio Files from Subtitles

## Overview

Add lyrics extraction from subtitle files and embed them as metadata into
the clean audio copies produced by `AudioMetadataPostProcessor`. Prefer
manual subtitles; fall back to auto-generated only when manual are absent.

---

## Step 1 — Find the subtitle file (`_find_subtitle`)

Add a new static method `_find_subtitle(info, final_path) -> Optional[Path]`
to `AudioMetadataPostProcessor`.

**Priority order** (manual first, auto second):

1. Check `info['requested_subtitles']` for manual sub entry (`en`):
   - The dict value has `'filepath'` — use it if the file exists on disk.
   - Manual subs are written when `writesubtitles: True` and the video has
     human-authored captions.
2. If no manual sub found, check `info['automatic_captions']` /
   `info['requested_subtitles']` for the auto-generated entry — yt-dlp
   merges both into `requested_subtitles` but marks auto with
   `"_type": "auto"` or similar.  As a simpler fallback, glob on disk:
   - `{stem}.en.vtt` (manual or auto — if step 1 didn't find manual,
     whatever is on disk is the auto sub)
   - `{stem}.en.srt`
3. Return `None` if nothing found.

**Location in code:** new `@staticmethod` on the class, right after
`_find_thumbnail`.

---

## Step 2 — Parse subtitles into plain text (`_parse_subtitles`)

Add `_parse_subtitles(sub_path: Path) -> Optional[str]`.

Handles both `.vtt` and `.srt`:

### VTT parsing
- Strip the `WEBVTT` header line and any blank lines before first cue.
- For each cue block:
  - Discard the timestamp line (`HH:MM:SS.mmm --> HH:MM:SS.mmm ...`).
  - Discard cue identifiers (numeric or named IDs on the line before
    the timestamp).
  - Strip HTML-like tags: `<c>`, `</c>`, `<i>`, `</i>`, `<b>`, `</b>`,
    `<c.colorXXXXXX>`, and any `<HH:MM:SS.mmm>` inline timestamps.
  - Keep the remaining text lines.

### SRT parsing
- For each cue block (separated by blank lines):
  - Discard the sequence number line (first line, all digits).
  - Discard the timestamp line (`HH:MM:SS,mmm --> HH:MM:SS,mmm`).
  - Keep the remaining text lines.
  - Strip `<i>`, `</i>`, `{\\an8}` style overrides.

### Deduplication (critical for auto-subs)
Auto-generated VTT from YouTube repeats lines across overlapping cues.
After collecting all text lines:
- Collapse consecutive identical lines into one.
- Strip leading/trailing whitespace per line.
- Join with `\n`.

### Size guard
If the resulting text exceeds 64 KB, truncate to 64 KB at the last
complete line boundary and append `\n[truncated]`. Some tagging
libraries and players can't handle arbitrarily large metadata values.

**Return:** the cleaned plain-text string, or `None` if the file
couldn't be parsed or produced no text.

---

## Step 3 — Generate synced LRC lyrics (`_to_lrc`)

Add `_to_lrc(sub_path: Path) -> Optional[str]`.

LRC format is widely supported by music players for synced lyrics display.

### Format
```
[mm:ss.xx]First line of text
[mm:ss.xx]Second line of text
```

### VTT → LRC
- For each cue, take the **start** timestamp.
- Convert `HH:MM:SS.mmm` → `[MM:SS.xx]` (total minutes, 2-digit
  centiseconds).
- Strip the same HTML tags as step 2.
- Dedup consecutive identical lines (same logic as step 2).

### SRT → LRC
- Same conversion: `HH:MM:SS,mmm` → `[MM:SS.xx]`.

### Size guard
Same 64 KB limit as step 2.

**Return:** LRC-formatted string or `None`.

---

## Step 4 — Wire into `_embed()` and `run()`

### In `run()`:
After resolving `thumb_path` and `chapters`, add:
```python
lyrics = self._parse_subtitles(sub_path) if sub_path else None
lyrics_synced = self._to_lrc(sub_path) if sub_path else None
```
Where `sub_path = self._find_subtitle(info, final_path)`.

Pass `lyrics` and `lyrics_synced` through to `_embed()`.

### In `_embed()`:
After the existing metadata tag loop, add:
```python
if lyrics:
    cmd.extend(['-metadata', f'lyrics={lyrics}'])
if lyrics_synced:
    cmd.extend(['-metadata', f'lyrics-eng={lyrics_synced}'])
```

This adds both plain-text (`lyrics`) and synced LRC (`lyrics-eng`) to
the ffmpeg command. Both are just metadata strings — no new inputs or
stream mappings.

### Metadata key per container
- **m4a (MP4):** `lyrics` maps to the iTunes `©lyr` atom.
- **webm/mkv (Matroska):** `LYRICS` Matroska tag.
- **ogg/opus:** `LYRICS` Vorbis comment.

ffmpeg normalizes the key name for each container, so using `lyrics`
in the `-metadata` flag works across all three.

---

## File changes summary

All changes are in `src/dihi/getvidyt.py`, within `AudioMetadataPostProcessor`:

| Method | Type | What it does |
|--------|------|-------------|
| `_find_subtitle` | new `@staticmethod` | Locate subtitle file (manual → auto fallback) |
| `_parse_subtitles` | new `@staticmethod` | VTT/SRT → deduplicated plain text |
| `_to_lrc` | new `@staticmethod` | VTT/SRT → synced `[mm:ss.xx]` LRC |
| `run` | modify | Call `_find_subtitle`, pass result to `_embed` |
| `_embed` | modify | Accept lyrics args, append `-metadata lyrics=...` to ffmpeg cmd |

No new files. No new dependencies (just `re` which is already imported).
