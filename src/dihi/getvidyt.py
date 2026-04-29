#!/usr/bin/env python3
import argparse
import glob as _glob
import math
import re
import shutil
import subprocess as _subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Union

from yt_dlp import YoutubeDL
from yt_dlp.postprocessor import PostProcessor


def _find_deno_path() -> str:
    """Find deno binary path, checking common locations and PATH."""
    # First check if deno is in PATH
    deno_in_path = shutil.which("deno")
    if deno_in_path:
        return deno_in_path

    # Check common installation locations
    common_paths = [
        "/root/.deno/bin/deno",  # Docker/root install
        Path.home() / ".deno" / "bin" / "deno",  # User install
        "./venv/bin/deno/bin/deno",  # Local venv install
    ]

    for path in common_paths:
        path = Path(path)
        if path.exists():
            return str(path)

    # Fall back to "deno" and let subprocess handle PATH resolution
    return "deno"

def _datetime_now() -> str:
    """Return current UTC time as an ISO 8601 string for sidecar timestamps."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


_SUB_TIMESTAMP_RE = re.compile(r'^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->')
_SUB_TAG_RE = re.compile(r'<[^>]+>')


def _sub_to_text(sub_path: Path) -> Optional[str]:
    """Extract deduplicated plain text from a VTT or SRT subtitle file."""
    try:
        raw = sub_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None

    lines = []
    prev = None
    for line in raw.splitlines():
        line = line.strip()
        # skip header, metadata, timestamps, cue numbers, and blank lines
        if (not line
                or line.startswith('WEBVTT')
                or line.startswith('Kind:')
                or line.startswith('Language:')
                or line.startswith('NOTE')
                or _SUB_TIMESTAMP_RE.match(line)
                or line.isdigit()):
            continue
        clean = _SUB_TAG_RE.sub('', line).strip()
        if clean and clean != prev:
            lines.append(clean)
            prev = clean

    return '\n'.join(lines) if lines else None


YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAUSIBLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def _parse_archive_line(line: str) -> Optional[str]:
    """Return the YouTube video ID from a yt-dlp archive line, or None."""
    s = line.strip()
    if not s:
        return None
    parts = s.split()
    if len(parts) < 2 or parts[0].lower() != "youtube":
        return None
    return parts[1] or None


def load_archive(path: Union[str, Path]) -> set:
    """Return the set of video IDs recorded in a yt-dlp archive file.

    Safe to call when the file does not exist — returns an empty set.
    """
    p = Path(path).expanduser().resolve()
    ids: set = set()
    if not p.exists():
        return ids
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            vid = _parse_archive_line(line)
            if vid:
                ids.add(vid)
    return ids


class AudioMetadataPostProcessor(PostProcessor):
    """Create metadata-enriched audio files from kept pre-merge streams.

    yt-dlp's built-in postprocessors only operate on the final merged output.
    With ``keepvideo: True`` the original audio streams (``.f140.m4a``,
    ``.f251.webm``, etc.) are preserved but receive no embedded metadata.

    This PP runs last, finds every kept audio sidecar, and writes a **new**
    copy with the ``.f<id>`` stripped from the filename (e.g.
    ``Title [abc].f140.m4a`` → ``Title [abc].m4a``) while embedding tags,
    cover art, and chapter markers.  The originals are left untouched.
    """

    _AUDIO_EXTS = ('.m4a', '.webm', '.opus', '.ogg')
    _FORMAT_ID_RE = re.compile(r'\.f\d+(?=\.\w+$)')
    _THUMB_MIME = {
        '.png': 'image/png', '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg', '.webp': 'image/webp',
    }

    def run(self, info):
        final = info.get('filepath', '')
        if not final:
            return [], info

        final_path = Path(final)
        audio_files = self._find_audio_files(info, final_path)
        if not audio_files:
            return [], info

        thumb_path = self._find_thumbnail(info, final_path)
        sub_path = self._find_subtitle(info, final_path)
        chapters = info.get('chapters') or []

        for src in audio_files:
            dst = self._clean_filename(src)
            if dst.exists():
                self.to_screen(f'Already exists, skipping: {dst.name}')
                continue
            self._embed(info, src, dst, thumb_path, chapters, sub_path)

        return [], info

    # ------------------------------------------------------------------
    def _find_audio_files(self, info: dict, final_path: Path) -> list:
        # Collect format IDs that are audio-only (no video)
        audio_fids = set()
        for fmt in info.get('requested_formats') or []:
            if fmt.get('acodec', 'none') != 'none' and fmt.get('vcodec', 'none') in ('none', None):
                fid = str(fmt.get('format_id', ''))
                if fid:
                    audio_fids.add(fid)

        results = []
        stem_escaped = _glob.escape(str(final_path.with_suffix('')))
        for ext in self._AUDIO_EXTS:
            for p in _glob.glob(stem_escaped + '.f*' + ext):
                path = Path(p)
                # Extract format ID using regex (suffixes[0] is wrong
                # when the filename contains dots from channel/date/title)
                m = self._FORMAT_ID_RE.search(path.name)
                fid = m.group(0)[2:] if m else ''  # strip leading '.f'
                if fid in audio_fids:
                    results.append(path)
        return results

    @classmethod
    def _clean_filename(cls, src: Path) -> Path:
        """``Title [id].out.f140.m4a`` → ``Title [id].out.m4a``."""
        return src.with_name(cls._FORMAT_ID_RE.sub('', src.name))

    @staticmethod
    def _find_subtitle(info: dict, final_path: Path) -> Optional[Path]:
        """Find the first VTT or SRT subtitle file for this download."""
        for lang, sub in (info.get('requested_subtitles') or {}).items():
            fp = sub.get('filepath')
            if fp and Path(fp).exists() and sub.get('ext') in ('vtt', 'srt'):
                return Path(fp)
        # Fallback: glob next to the output (prefer vtt, then srt)
        base = _glob.escape(str(final_path.with_suffix('')))
        for ext in ('vtt', 'srt'):
            for p in sorted(_glob.glob(base + f'.*.{ext}')):
                return Path(p)
        return None

    @staticmethod
    def _find_thumbnail(info: dict, final_path: Path) -> Optional[Path]:
        for thumb in reversed(info.get('thumbnails') or []):
            fp = thumb.get('filepath')
            if fp and Path(fp).exists():
                return Path(fp)
        base = str(final_path.with_suffix(''))
        for ext in ('.png', '.jpg', '.jpeg', '.webp'):
            p = Path(base + ext)
            if p.exists():
                return p
        return None

    # ------------------------------------------------------------------
    def _embed(self, info, src: Path, dst: Path, thumb_path, chapters,
               sub_path: Optional[Path] = None):
        ext = dst.suffix.lower()
        is_mp4 = ext in ('.m4a', '.mp4')
        is_matroska = ext in ('.webm', '.mkv')
        is_ogg = ext in ('.opus', '.ogg')

        # m4a/mp4 only supports PNG/JPEG cover art — convert if needed
        converted_thumb = None
        if thumb_path and is_mp4 and thumb_path.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
            converted_thumb = thumb_path.with_suffix('.cover.png')
            if not converted_thumb.exists():
                try:
                    _subprocess.run(
                        ['ffmpeg', '-y', '-i', str(thumb_path), str(converted_thumb)],
                        check=True, capture_output=True)
                except _subprocess.CalledProcessError:
                    converted_thumb = None
            if converted_thumb and converted_thumb.exists():
                thumb_path = converted_thumb

        cmd = ['ffmpeg', '-y', '-i', str(src)]
        input_count = 1

        # -- cover art (mp4: attached_pic video stream) --
        thumb_idx = None
        if thumb_path and is_mp4 and thumb_path.suffix.lower() in ('.png', '.jpg', '.jpeg'):
            cmd.extend(['-i', str(thumb_path)])
            thumb_idx = input_count
            input_count += 1

        # -- chapters via ffmetadata --
        chap_file = None
        chap_idx = None
        if chapters:
            chap_file = str(dst) + '.ffmeta'
            with open(chap_file, 'w') as f:
                f.write(';FFMETADATA1\n')
                for ch in chapters:
                    s = int(ch['start_time'] * 1000)
                    e = int(ch['end_time'] * 1000)
                    t = (ch.get('title', '')
                         .replace('\\', '\\\\')
                         .replace('=', r'\=')
                         .replace(';', r'\;')
                         .replace('#', r'\#')
                         .replace('\n', r'\n'))
                    f.write(f'\n[CHAPTER]\nTIMEBASE=1/1000\n'
                            f'START={s}\nEND={e}\ntitle={t}\n')
            cmd.extend(['-f', 'ffmetadata', '-i', chap_file])
            chap_idx = input_count
            input_count += 1

        # -- stream mappings --
        cmd.extend(['-map', '0:a'])
        if thumb_idx is not None:
            cmd.extend(['-map', f'{thumb_idx}:0'])

        # -- codecs --
        cmd.extend(['-c:a', 'copy'])
        if thumb_idx is not None:
            cmd.extend(['-c:v', 'copy', '-disposition:v:0', 'attached_pic'])

        # -- cover art (matroska/webm: attachment) --
        if thumb_path and is_matroska:
            mime = self._THUMB_MIME.get(
                thumb_path.suffix.lower(), 'image/png')
            cmd.extend(['-attach', str(thumb_path),
                        '-metadata:s:t', f'mimetype={mime}'])

        if chap_idx is not None:
            cmd.extend(['-map_chapters', str(chap_idx)])

        # -- metadata tags --
        desc = info.get('description') or ''
        if len(desc) > 4000:
            desc = desc[:4000] + '\n[truncated]'
        for key, val in [
            #('title', info.get('title')),
            #('artist', info.get('uploader') or info.get('channel')),
            #('album_artist', info.get('channel')),
            #('album', info.get('playlist_title') or info.get('channel')),
            ('title',        info.get('track') or info.get('alt_title') or info.get('title')),
            ('artist',       info.get('artist') or info.get('creator') or info.get('uploader') or info.get('channel')),
            ('album_artist', info.get('artist') or info.get('channel')),
            ('album',        info.get('album') or info.get('playlist_title') or info.get('channel')),
            ('date', info.get('upload_date')),
            ('comment', info.get('webpage_url')),
            ('description', desc),
            ('episode_id', info.get('id')),
            ('track', str(info['playlist_index'])
             if info.get('playlist_index') else None),
        ]:
            if val:
                cmd.extend(['-metadata', f'{key}={val}'])

        # -- lyrics via ffmpeg metadata for webm/mkv --
        if is_matroska and sub_path:
            webm_lyrics = _sub_to_text(sub_path)
            if webm_lyrics:
                cmd.extend(['-metadata', f'LYRICS={webm_lyrics}'])

        cmd.append(str(dst))

        self.to_screen(f'Creating {dst.name}')
        try:
            _subprocess.run(cmd, check=True, capture_output=True)
            # -- embed lyrics via mutagen for m4a (©lyr atom) --
            if is_mp4 and sub_path:
                lyrics = _sub_to_text(sub_path)
                if lyrics:
                    try:
                        from mutagen.mp4 import MP4
                        mp4 = MP4(str(dst))
                        mp4['\xa9lyr'] = [lyrics]
                        mp4.save()
                        self.to_screen(f'Lyrics embedded: {dst.name}')
                    except Exception as e:
                        self.to_screen(f'Lyrics failed: {e}')
            # -- embed lyrics via mutagen for opus/ogg (LYRICS vorbis comment) --
            if is_ogg and sub_path:
                lyrics = _sub_to_text(sub_path)
                if lyrics:
                    try:
                        if ext == '.opus':
                            from mutagen.oggopus import OggOpus
                            audio = OggOpus(str(dst))
                        else:
                            from mutagen.oggvorbis import OggVorbis
                            audio = OggVorbis(str(dst))
                        audio['LYRICS'] = [lyrics]
                        audio.save()
                        self.to_screen(f'Lyrics embedded: {dst.name}')
                    except Exception as e:
                        self.to_screen(f'Lyrics failed: {e}')
            self.to_screen(f'Done: {dst.name}')
        except _subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors='replace')
            # ffmpeg prints its banner first; the real error is at the end
            self.to_screen(f'Failed: {stderr[-500:]}')
            if dst.exists():
                dst.unlink()
        finally:
            if chap_file and Path(chap_file).exists():
                Path(chap_file).unlink()
            if converted_thumb and converted_thumb.exists():
                converted_thumb.unlink()


class StandaloneAudioMetaPP(AudioMetadataPostProcessor):
    """AudioMetadataPostProcessor that prints to stdout instead of yt-dlp."""

    def to_screen(self, msg, *args, **kwargs):
        print(f"[AudioMeta] {msg}")

    @classmethod
    def from_info_json(cls, info_json_path: Path) -> tuple:
        """Load an .info.json file and resolve sibling file paths.

        Returns (info_dict, resolved) where *resolved* is True if a merged
        output file was found on disk (needed for audio-sidecar discovery).
        """
        import json

        info_json_path = Path(info_json_path).expanduser().resolve()
        if not info_json_path.exists():
            raise FileNotFoundError(info_json_path)

        info = json.loads(info_json_path.read_text(encoding="utf-8"))

        # Derive the common stem: strip ".info.json" suffix
        name = info_json_path.name
        if name.endswith(".info.json"):
            stem = name[: -len(".info.json")]
        else:
            stem = info_json_path.stem
        parent = info_json_path.parent
        stem_escaped = _glob.escape(str(parent / stem))

        # Resolve the merged output file (e.g. <stem>.mkv)
        merged_path = None
        for ext in (".mkv", ".mp4", ".webm"):
            candidate = parent / (stem + ext)
            if candidate.exists():
                merged_path = candidate
                break
        if merged_path is None:
            # Try glob as fallback
            for p in _glob.glob(stem_escaped + ".*"):
                pp = Path(p)
                if pp.suffix.lower() in (".mkv", ".mp4", ".webm") and ".f" not in pp.suffixes[-2:-1]:
                    merged_path = pp
                    break

        if merged_path:
            info["filepath"] = str(merged_path)

        # Re-resolve thumbnail paths to files on disk next to the .info.json
        for thumb in info.get("thumbnails") or []:
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                candidate = parent / (stem + ext)
                if candidate.exists():
                    thumb["filepath"] = str(candidate)
                    break

        # Re-resolve subtitle paths
        for _lang, sub in (info.get("requested_subtitles") or {}).items():
            for ext in ("vtt", "srt"):
                for p in sorted(_glob.glob(stem_escaped + f".*.{ext}")):
                    sub["filepath"] = p
                    sub["ext"] = ext
                    break

        return info, merged_path is not None

    @classmethod
    def process_single(cls, info_json_path: Path) -> bool:
        """Process a single .info.json file. Returns True on success."""
        info_json_path = Path(info_json_path).expanduser().resolve()
        try:
            info, resolved = cls.from_info_json(info_json_path)
        except (FileNotFoundError, Exception) as e:
            print(f"[AudioMeta] ERROR loading {info_json_path}: {e}")
            return False

        if not resolved:
            print(f"[AudioMeta] SKIP {info_json_path.name}: no merged output file found")
            return False

        pp = cls()
        files_to_delete, info = pp.run(info)
        return True

    @classmethod
    def process_directory(cls, directory: Path, recursive: bool = True) -> int:
        """Process all .info.json files in a directory. Returns count processed."""
        directory = Path(directory).expanduser().resolve()
        if not directory.is_dir():
            print(f"[AudioMeta] ERROR: {directory} is not a directory")
            return 0

        pattern = "**/*.info.json" if recursive else "*.info.json"
        info_files = sorted(directory.glob(pattern))

        if not info_files:
            print(f"[AudioMeta] No .info.json files found in {directory}")
            return 0

        print(f"[AudioMeta] Found {len(info_files)} .info.json file(s)")
        processed = 0
        for i, info_json in enumerate(info_files, 1):
            print(f"\n[AudioMeta] [{i}/{len(info_files)}] {info_json.parent.name}/{info_json.name}")
            if cls.process_single(info_json):
                processed += 1

        print(f"\n[AudioMeta] Done: {processed}/{len(info_files)} processed")
        return processed


class MetadataSidecarPostProcessor(PostProcessor):
    """Write timestamped human-readable metadata sidecar files alongside downloads.

    YouTube metadata fields that appear stable can change over time:
      - Channel display names change on rebrands
      - @handles (uploader_id) can be changed by the creator
      - Video titles are frequently edited for SEO or corrections
      - Upload dates occasionally shift on re-uploads

    Embedding these values in folder or file names causes fragmentation:
    new downloads land in a new path while old files remain at the old one.
    Instead, this postprocessor writes them to small dot-files that live next
    to the content and are updated in place, keeping the folder structure
    permanently stable (channel_id/video_id/) while still being human-browsable.

    File format — each file is an append-only log of timestamped values:

        2026-04-29T12:34:56Z Never Gonna Give You Up
        2026-05-15T08:30:00Z Never Gonna Give You Up (Official Remaster)

    A new line is only appended when the value differs from the last recorded
    entry, so repeated downloads of the same video (e.g. during a backfill run
    without the archive) do not produce duplicate lines. When a value does
    change, both the old and new entries are preserved with their timestamps,
    giving a full audit trail of when the change was first observed.

    Channel-level files (written to <merged_dir>/<channel_id>/):
      .channel_name  — display name (info['channel'] or info['uploader'])
      .uploader_id   — @handle (info['uploader_id']); changes are trackable
      .uploader_name — raw uploader string (info['uploader'])

    Video-level files (written to the video subfolder from info['filepath']):
      .title_name    — video title (info['title'])
      .upload_date   — upload date in YYYYMMDD format (info['upload_date'])
    """

    def __init__(self, merged_dir: Path):
        super().__init__()
        self._merged_dir = Path(merged_dir)

    def run(self, info):
        channel_id = info.get('channel_id')
        filepath = info.get('filepath')
        video_dir = Path(filepath).parent if filepath else None
        channel_dir = self._merged_dir / channel_id if channel_id else None
        now = _datetime_now()

        if channel_dir:
            self._append_if_changed(
                channel_dir / '.channel_name',
                info.get('channel') or info.get('uploader'), now)
            self._append_if_changed(
                channel_dir / '.uploader_id',
                info.get('uploader_id'), now)
            self._append_if_changed(
                channel_dir / '.uploader_name',
                info.get('uploader'), now)

        if video_dir:
            self._append_if_changed(
                video_dir / '.title_name',
                info.get('title'), now)
            self._append_if_changed(
                video_dir / '.upload_date',
                info.get('upload_date'), now)

        return [], info

    @staticmethod
    def _append_if_changed(path: Path, value: str, timestamp: str) -> None:
        """Append '<timestamp> <value>' to path only if value differs from last entry."""
        if not value:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        last = None
        if path.exists():
            for line in reversed(path.read_text(encoding='utf-8').splitlines()):
                line = line.strip()
                if line:
                    # First token is the timestamp; everything after is the value
                    parts = line.split(' ', 1)
                    last = parts[1] if len(parts) == 2 else line
                    break
        if value != last:
            with path.open('a', encoding='utf-8') as f:
                f.write(f'{timestamp} {value}\n')


def to_youtube_url(user_input: str) -> str:
    """
    Accepts:
      - full URL
      - bare video id (11 chars)
      - bare playlist id (>= ~10 chars)
    Returns a URL-like string yt-dlp can handle.
    """
    s = user_input.strip()

    if "://" in s or s.startswith("www."):
        return "https://" + s if s.startswith("www.") else s

    if YOUTUBE_VIDEO_ID_RE.match(s):
        return f"https://www.youtube.com/watch?v={s}"

    if PLAUSIBLE_ID_RE.match(s):
        return f"https://www.youtube.com/playlist?list={s}"

    return s


def build_ydl_opts(
    merged_dir: Union[str, Path] = "merged",
    archive: Union[str, Path] = "archive.txt",
    *,
    cookies_browser: Optional[str] = None,
    no_js: bool = False,
    extra_opts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a yt-dlp options dict suitable for YoutubeDL(...).
    You can pass extra_opts to override/add any keys.
    """
    merged_dir = Path(merged_dir).expanduser().resolve()
    merged_dir.mkdir(parents=True, exist_ok=True)

    archive_path = Path(archive).expanduser().resolve()
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    ydl_opts: Dict[str, Any] = {
        # --- Reliability / behavior ---
        "yes_playlist": True,
        "ignoreerrors": True,
        "nooverwrites": True,
        "continuedl": True,
        "concurrent_fragment_downloads": 8,
        "retries": math.inf,
        "fragment_retries": math.inf,
        "download_archive": str(archive_path),

        "retry_sleep_functions": {"http": lambda n: min(60, 2 ** n)},

        # --- Delay between downloads ---
        "sleep_interval": 5,
        "max_sleep_interval": 12,

        # --- Cookies / UA ---
        # Enable cookies-from-browser if provided
        # (example: "firefox", or ("firefox", {"profile": "default-release"}) depending on your setup)
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",

        # --- Format / merge ---
        # "format": "399+140/299+140/137+140/298+140/136+140/135+140/134+140/133+140/160+140/best",
        "format": "bestvideo[height<=1080][vcodec^=av01]+251/bestvideo[height<=1080]+251/bestvideo[height<=1080]+bestaudio/best,140/bestaudio",
        "merge_output_format": "mkv",
        "keepvideo": True,

        # --- "All data" sidecars ---
        "writeinfojson": True,
        "writedescription": True,
        "writethumbnail": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        # Subtitle language selection strategy:
        #
        # "en" — requests English subtitles. yt-dlp automatically prefers manually
        #   uploaded subtitle tracks over YouTube's auto-generated speech-to-text
        #   when both exist for the same language code. This means creator-provided
        #   captions (more accurate, often including speaker labels and corrections)
        #   are used whenever available, with auto-generated as the fallback. No
        #   extra configuration is needed to express this preference — it is
        #   yt-dlp's built-in behaviour.
        #
        # "en-orig" — requests the original-language auto-generated track for
        #   non-English videos. When a video is in Spanish, French, Japanese, etc.,
        #   YouTube auto-transcribes the spoken audio into the source language; that
        #   transcript is labelled "en-orig" (the original before any translation).
        #   For English-language videos this track does not exist and is silently
        #   skipped. Including it here means non-English channels get a native-
        #   language transcript alongside the English one, at no cost for English
        #   channels.
        #
        # Why not "all"? Requesting all languages downloads every machine-translated
        #   variant YouTube generates (often 30–60 per video), which multiplies the
        #   file count dramatically without adding meaningful information — they are
        #   all machine-translated from the same "en-orig" source. The two-entry list
        #   below captures the highest-value tracks with at most 2 files per video.
        #
        # To backfill subtitles for already-downloaded videos without re-downloading:
        #   download_youtube(url, extra_opts={"skip_download": True,
        #       "download_archive": None, "subtitleslangs": ["en", "en-orig"]})
        "subtitleslangs": ["en", "en-orig"],
        "writeplaylistmetafiles": True,

        # --- Postprocessors (explicit — the Python API does NOT auto-create
        #     these from boolean flags like the CLI does) ---
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "png", "when": "before_dl"},
            {"key": "FFmpegEmbedSubtitle"},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True,
             "add_infojson": "if_exists"},
        ],

        # --- Output paths / template ---
        "paths": {"home": str(merged_dir)},
        # Output path design decisions:
        #
        # Folder structure: <channel_id>/<video_id>/
        #   Both identifiers are assigned by YouTube and never change, making the
        #   folder hierarchy permanently stable regardless of channel renames, title
        #   edits, or video re-uploads. Using the human-readable channel name or
        #   title as a folder would cause splits when creators rebrand or edit
        #   metadata (new downloads land in a new folder; old files stay in the old
        #   one with no automatic reconciliation). The @handle (uploader_id) is also
        #   excluded for the same reason — YouTube now allows creators to change it.
        #
        # Filename: <upload_date>.<title> [<video_id>].out.<ext>
        #   The date prefix sorts chronologically in any file browser. The title and
        #   video ID are repeated in the filename (even though they appear in the
        #   folder path) so that each file is self-describing when viewed in isolation
        #   — e.g. when sorting search results or sharing a single file.
        #
        #   The ".out." infix between title and extension is load-bearing: it is the
        #   anchor used by AudioMetadataPostProcessor's regex (\.f\d+(?=\.\w+$)) to
        #   strip format IDs from sidecar filenames (e.g. ".out.f140.m4a" → ".out.m4a").
        #   Do not remove it.
        #
        #   The former "CID_<channel_id>." prefix has been removed because the
        #   channel ID is already encoded in the parent folder — repeating it in
        #   every filename added length without new information.
        #
        # Human-readable names: see MetadataSidecarPostProcessor, which writes
        #   .channel_name / .uploader_id / .uploader_name into the channel folder and
        #   .title_name / .upload_date into the video folder as timestamped log files.
        "outtmpl": "%(channel_id)s/%(id)s/%(upload_date|NA)s.%(title)s [%(id)s].out.%(ext)s",
    }

    cookies_file = archive_path.parent / "cookies.txt"
    if cookies_file.exists():
        ydl_opts["cookiefile"] = str(cookies_file)
    if cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_browser,)
    if not no_js:
        # Your yt-dlp build expects dict {runtime: {config}}
        deno_path = _find_deno_path()
        ydl_opts["js_runtimes"] = {"deno": {"path": deno_path}}
        ydl_opts["remote_components"] = ["ejs:github", "ejs:npm"]
    if extra_opts:
        # Allow caller to override anything (format, outtmpl, paths, etc.)
        ydl_opts.update(extra_opts)

    return ydl_opts


def download_youtube(
    target: str,
    *,
    merged_dir: Union[str, Path] = "merged",
    archive: Union[str, Path] = "archive.txt",
    cookies_browser: Optional[str] = None,
    no_js: bool = False,
    extra_opts: Optional[Dict[str, Any]] = None,
    quiet: bool = False,
    audio_meta: bool = False,
) -> int:
    """
    Callable function for external programs.

    Parameters
    ----------
    target : str
        YouTube URL, video ID, or playlist ID.
    merged_dir : str|Path
        Base output directory (mapped to yt-dlp "home" path).
    archive : str|Path
        download-archive file.
    cookies_browser : str|None
        e.g. "firefox" (enables cookies-from-browser). Leave None to disable.
    no_js : bool
        If True, don't configure js_runtimes.
    extra_opts : dict|None
        Extra yt-dlp options to add/override.
    quiet : bool
        If True, suppress yt-dlp output (still returns status code).
    audio_meta : bool
        If True, run AudioMetadataPostProcessor to create clean audio copies
        with embedded metadata. Default is False (skip).

    Returns
    -------
    int
        yt-dlp download() return code (0 success, nonzero errors).
    """
    target_url = to_youtube_url(target)

    ydl_opts = build_ydl_opts(
        merged_dir=merged_dir,
        archive=archive,
        cookies_browser=cookies_browser,
        no_js=no_js,
        extra_opts=extra_opts,
    )

    if quiet:
        ydl_opts.setdefault("quiet", True)
        ydl_opts.setdefault("no_warnings", True)

    with YoutubeDL(ydl_opts) as ydl:
        # FFmpegMetadataPP._options('m4a') calls stream_copy_opts(False)
        # without ext=, so subtitles embedded by EmbedSubtitle are mapped
        # (-map 0) but no subtitle codec is specified, causing ffmpeg to
        # fail with "Encoder not found".  Patch _options on the instance
        # to copy subtitle streams as-is (they're already mov_text from
        # the EmbedSubtitle step).
        for pps in ydl._pps.values():
            for pp in pps:
                if type(pp).__name__ == 'FFmpegMetadataPP':
                    _orig_options = pp._options
                    def _patched_options(target_ext, _orig=_orig_options):
                        yield from _orig(target_ext)
                        if target_ext in ('m4a', 'mp4'):
                            yield from ('-c:s', 'copy')
                    pp._options = _patched_options
                    break
        if audio_meta:
            ydl.add_post_processor(AudioMetadataPostProcessor(), when='post_process')
        ydl.add_post_processor(
            MetadataSidecarPostProcessor(merged_dir), when='post_process'
        )
        return ydl.download([target_url])


def _run_download(args) -> int:
    """Execute the download subcommand."""
    print(f"Target: {to_youtube_url(args.target)}")
    print(f"Output base: {Path(args.merged_dir).expanduser().resolve()}")
    print(f"Archive: {Path(args.archive).expanduser().resolve()}")
    if args.audio_meta:
        print("Audio metadata post-processing: enabled")

    return download_youtube(
        args.target,
        merged_dir=args.merged_dir,
        archive=args.archive,
        cookies_browser=args.cookies_browser or None,
        no_js=args.no_js,
        quiet=args.quiet,
        audio_meta=args.audio_meta,
    )


def _run_audio_meta(args) -> int:
    """Execute the audio-meta subcommand."""
    target = Path(args.path).expanduser().resolve()

    if target.is_file() and target.name.endswith(".info.json"):
        ok = StandaloneAudioMetaPP.process_single(target)
        return 0 if ok else 1
    elif target.is_dir():
        count = StandaloneAudioMetaPP.process_directory(
            target, recursive=not args.no_recursive,
        )
        return 0 if count > 0 else 1
    else:
        print(f"Error: {target} is not a directory or .info.json file")
        return 1


def _add_download_args(parser):
    """Add download arguments to a parser."""
    parser.add_argument("target", help="YouTube URL or ID (video id or playlist id).")
    parser.add_argument("--archive", default="archive.txt", help="Download archive file (default: archive.txt).")
    parser.add_argument("--merged-dir", default="merged", help='Output base directory for "home" path (default: merged).')
    parser.add_argument("--cookies-browser", default="", help='Enable cookies-from-browser (e.g. "firefox"). Disabled by default.')
    parser.add_argument("--no-js", action="store_true", help="Disable js_runtimes config.")
    parser.add_argument("--quiet", action="store_true", help="Suppress yt-dlp output.")
    parser.add_argument("--audio-meta", action="store_true", help="Create clean audio copies with embedded metadata (off by default).")


def main() -> int:
    import sys

    # Backwards compatibility: bare `python getvidyt.py <target>` with no subcommand.
    # Detect this by checking if the first positional arg is NOT a known subcommand.
    _SUBCOMMANDS = {"download", "audio-meta"}
    argv = sys.argv[1:]
    if argv and argv[0] not in _SUBCOMMANDS and not argv[0].startswith("-"):
        # Treat as implicit "download" subcommand
        argv = ["download"] + argv

    ap = argparse.ArgumentParser(
        description="Download YouTube video/playlist and manage audio metadata."
    )
    sub = ap.add_subparsers(dest="command")

    # -- download subcommand --
    dl = sub.add_parser("download", help="Download a YouTube video or playlist.")
    _add_download_args(dl)

    # -- audio-meta subcommand --
    am = sub.add_parser("audio-meta", help="Create clean audio copies from already-downloaded files.")
    am.add_argument("path", help="Directory to scan or a single .info.json file.")
    am.add_argument("--no-recursive", action="store_true", help="Don't recurse into subdirectories (default: recurse).")

    args = ap.parse_args(argv)

    if args.command == "download":
        return _run_download(args)
    elif args.command == "audio-meta":
        return _run_audio_meta(args)
    else:
        ap.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())










#from ydl_module import download_youtube  # whatever filename you saved as

#rc = download_youtube(
#    "dQw4w9WgXcQ",
#    merged_dir="merged",
#    archive="archive.txt",
#    cookies_browser="firefox",   # or None
#    no_js=False,
#    extra_opts={
        # override anything if you want:
        # "format": "bestvideo+bestaudio/best",
#    },
#)
#print("Return code:", rc)
