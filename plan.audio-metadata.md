# Plan: Extract AudioMetadataPostProcessor from Main Download Pipeline

## Goal

Decouple `AudioMetadataPostProcessor` from the download pipeline so that:

1. **Downloads skip audio metadata by default** — the PP is no longer part of the standard pipeline
2. Downloads can **opt in** to audio metadata via a `--audio-meta` flag
3. Audio metadata can be **run standalone** on already-downloaded files using their `.info.json` sidecars

---

## Current State

- `AudioMetadataPostProcessor` is unconditionally added in `download_youtube()` at `getvidyt.py:492`
- It relies on the yt-dlp `info` dict for: title, uploader, channel, upload_date, webpage_url, description, playlist metadata, chapters, requested_formats (to find audio format IDs), requested_subtitles, thumbnails
- Fortunately, `writeinfojson: True` is set, so `.info.json` files are already saved to disk with all of this data
- The PP finds audio sidecars (`.f<id>.m4a`, `.f<id>.webm`) next to the merged output, finds thumbnails and subtitles on disk via globbing, then creates clean audio copies with embedded metadata

### Key Insight

The `.info.json` file written by yt-dlp contains everything `AudioMetadataPostProcessor` needs. The standalone mode can load this JSON, reconstruct the necessary `info` dict fields, resolve file paths relative to the `.info.json` location, and run the same embedding logic.

---

## Changes

### 1. Change default: skip audio metadata during downloads

**File: `src/dihi/getvidyt.py`**

- Add `audio_meta: bool = False` parameter to `download_youtube()`
- Wrap the `ydl.add_post_processor(AudioMetadataPostProcessor(), when='post_process')` call in `if audio_meta:`
- Add `--audio-meta` flag to `main()` argparse and pass it through
- **Default is OFF** — downloads no longer create clean audio copies unless explicitly requested

This makes downloads faster and lighter by default. The audio metadata step can be done later via the standalone `audio-meta` subcommand on existing files.

### 2. Update the API download worker

**File: `src/dihi/app3.py`**

- The API call `getvidyt.download_youtube(video_id)` now skips audio metadata by default — no code change needed in `app3.py` since the new default (`audio_meta=False`) applies automatically
- If audio metadata is desired from the API in the future, a query parameter or JSON body field can be added to pass `audio_meta=True` through to `download_youtube()`

### 3. Refactor `AudioMetadataPostProcessor` to support standalone operation

**File: `src/dihi/getvidyt.py`**

The core change: make `AudioMetadataPostProcessor` work without being inside a yt-dlp session. Add a class method that constructs the processor and runs it from an `info.json` file.

#### 3a. Add `@classmethod from_info_json(cls, info_json_path: Path) -> tuple[dict, Path]`

Static helper that:
1. Loads the `.info.json` file
2. Resolves `filepath` — the merged output path. This is reconstructed from the `outtmpl` pattern or found by globbing for `*[<id>].out.mkv` / `*[<id>].out.*` next to the `.info.json`
3. Patches `info['filepath']` to point to the resolved path
4. Patches `info['thumbnails']` entries — their `filepath` values reference absolute paths from the original download machine. Resolve them by looking for thumbnail files on disk next to the `.info.json` (glob for `*.png`, `*.jpg`, `*.webp`)
5. Returns `(info_dict, final_path)`

#### 3b. Add `@classmethod process_directory(cls, directory: Path, recursive: bool = True)`

Walks a directory, finds all `.info.json` files, and for each one:
1. Calls `from_info_json()` to load metadata and resolve paths
2. Creates an `AudioMetadataPostProcessor` instance (with a no-op `to_screen` since we're outside yt-dlp)
3. Calls `run(info)` on it
4. Reports results to stdout

This is the primary standalone entrypoint.

#### 3c. Add `@classmethod process_single(cls, info_json_path: Path)`

Processes a single `.info.json` file. Useful for targeted re-runs.

### 4. Add standalone CLI entrypoint

**File: `src/dihi/getvidyt.py`** (extend existing `main()`)

Add a subcommand or separate mode. Two approaches:

**Option A — Subcommands via argparse** (recommended):
```
python getvidyt.py download <target> [--archive ...] [--audio-meta] ...
python getvidyt.py audio-meta <path> [--recursive]
```

Where `audio-meta` mode:
- `<path>` can be a directory (process all `.info.json` files found) or a single `.info.json` file
- `--recursive` (default True) controls whether to walk subdirectories

**Option B — Separate script**:
Create `src/dihi/audio_meta.py` as a thin wrapper that imports `AudioMetadataPostProcessor` and calls `process_directory()` / `process_single()`. This avoids changing the existing `getvidyt.py` CLI interface.

**Recommendation: Option A** — keeps everything in one module, the `AudioMetadataPostProcessor` class already lives in `getvidyt.py`, and subcommands are a clean way to extend the CLI.

For backwards compatibility, bare `python getvidyt.py <target>` (no subcommand) should continue to work as before (treated as `download`).

### 5. Handle `to_screen` outside yt-dlp context

`AudioMetadataPostProcessor` inherits from `yt_dlp.postprocessor.PostProcessor` which provides `to_screen()`. When running standalone (not attached to a `YoutubeDL` instance), `to_screen()` will fail because `self._downloader` is `None`.

Fix: Override `to_screen` in the standalone path. The simplest approach is a small wrapper:

```python
class StandaloneAudioMetaPP(AudioMetadataPostProcessor):
    """AudioMetadataPostProcessor that prints to stdout instead of yt-dlp."""
    def to_screen(self, msg, *args, **kwargs):
        print(f"[AudioMeta] {msg}")
```

Or, set `self._downloader` to a minimal stub. The wrapper subclass is cleaner.

### 6. Resolving file paths from `.info.json`

The `.info.json` contains:
- `id` — video ID
- `title`, `uploader`, `channel`, `channel_id`, `upload_date` — metadata
- `requested_formats` — list of format dicts with `format_id`, `acodec`, `vcodec`
- `chapters` — chapter list
- `requested_subtitles` — dict with subtitle info (but `filepath` may be stale)
- `thumbnails` — list with thumbnail info (but `filepath` may be stale)
- `filepath` or `_filename` — the final output path (may be stale/absolute)

**Path resolution strategy:**
1. The `.info.json` is always next to the output files (same directory, same stem)
2. Derive the stem from the `.info.json` filename: strip `.info.json` suffix
3. The merged output is `<stem>.mkv` (or other merge format)
4. Thumbnails: glob `<stem>.png`, `<stem>.jpg`, `<stem>.webp` in same directory
5. Subtitles: glob `<stem>.*.vtt`, `<stem>.*.srt` in same directory
6. Audio sidecars: glob `<stem>.f*.m4a`, `<stem>.f*.webm`, etc.

This approach is filesystem-based and doesn't rely on stale absolute paths in the JSON.

**Important:** The `.info.json` filename follows the same `outtmpl` as all other files, so stripping `.info.json` gives the correct stem that matches all sidecars.

---

## Detailed Code Changes

### `src/dihi/getvidyt.py`

| Location | Change |
|----------|--------|
| `download_youtube()` signature | Add `audio_meta: bool = False` parameter (default OFF) |
| `download_youtube()` body, line ~492 | Wrap `add_post_processor` in `if audio_meta:` |
| `AudioMetadataPostProcessor` class | Add `from_info_json()` classmethod |
| `AudioMetadataPostProcessor` class | Add `process_directory()` classmethod |
| `AudioMetadataPostProcessor` class | Add `process_single()` classmethod |
| New class `StandaloneAudioMetaPP` | Subclass with `to_screen` override for standalone use |
| `main()` | Refactor to use subcommands (`download` + `audio-meta`), keep bare-target compat |
| `main()` argparse | Add `--audio-meta` opt-in flag to download subcommand |

### `src/dihi/app3.py`

| Location | Change |
|----------|--------|
| (none required) | Audio metadata is now skipped by default — no changes needed |

---

## Example Usage After Implementation

```bash
# Download WITHOUT audio metadata (new default)
python getvidyt.py download dQw4w9WgXcQ

# Backwards-compatible (no subcommand = download, still no audio meta)
python getvidyt.py dQw4w9WgXcQ

# Download WITH audio metadata post-processing (opt-in)
python getvidyt.py download dQw4w9WgXcQ --audio-meta

# Run audio metadata on a single video's info.json
python getvidyt.py audio-meta merged/UCxxx/dQw4w9WgXcQ/CID_UCxxx.20091025.Rick\ Astley\ -\ Never\ Gonna\ Give\ You\ Up\ \[dQw4w9WgXcQ\].out.info.json

# Run audio metadata on an entire directory tree
python getvidyt.py audio-meta merged/

# Run on a specific channel's downloads
python getvidyt.py audio-meta merged/UCuAXFkgsw1L7xaCfnd5JJOw/
```

---

## Edge Cases & Considerations

1. **Already-processed files**: `_clean_filename()` produces the output path. If it already exists, the PP skips it (existing behavior at line 110). This means re-running is safe — it won't overwrite existing clean audio files.

2. **Missing sidecars**: If `keepvideo: True` wasn't set during download, audio sidecars won't exist. The PP gracefully returns an empty list from `_find_audio_files()`. The standalone tool should log a warning.

3. **Missing thumbnail/subtitle**: Already handled gracefully (returns `None`, embedding is skipped).

4. **`requested_formats` in `.info.json`**: This field is present in `.info.json` and contains the format IDs needed to identify audio sidecars. Verified by yt-dlp's `writeinfojson` behavior.

5. **`_downloader` dependency**: Beyond `to_screen`, `PostProcessor` uses `self._downloader` in some utility methods. The `run()` method as currently written only calls `self.to_screen()` and `self._find_*()` / `self._embed()` — all of which are self-contained. The `StandaloneAudioMetaPP` subclass handles the `to_screen` case.

6. **Concurrent runs**: The standalone tool processes files sequentially. No thread safety concerns since it doesn't share state with the server.
