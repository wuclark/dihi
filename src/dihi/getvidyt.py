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

YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAUSIBLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


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
        chapters = info.get('chapters') or []

        for src in audio_files:
            dst = self._clean_filename(src)
            if dst.exists():
                self.to_screen(f'Already exists, skipping: {dst.name}')
                continue
            self._embed(info, src, dst, thumb_path, chapters)

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
                # Extract format ID from .f<id>.ext pattern
                fid = path.suffixes[0].lstrip('.f') if len(path.suffixes) >= 2 else ''
                if fid in audio_fids:
                    results.append(path)
        return results

    @classmethod
    def _clean_filename(cls, src: Path) -> Path:
        """``Title [id].f140.m4a`` → ``Title [id].ebmd.m4a``."""
        return src.with_name(cls._FORMAT_ID_RE.sub('.ebmd', src.name))

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
    def _embed(self, info, src: Path, dst: Path, thumb_path, chapters):
        ext = dst.suffix.lower()
        is_mp4 = ext in ('.m4a', '.mp4')
        is_matroska = ext in ('.webm', '.mkv')

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
            ('title', info.get('title')),
            ('artist', info.get('uploader') or info.get('channel')),
            ('album_artist', info.get('channel')),
            ('album', info.get('playlist_title') or info.get('channel')),
            ('date', info.get('upload_date')),
            ('comment', info.get('webpage_url')),
            ('description', desc),
            ('episode_id', info.get('id')),
            ('track', str(info['playlist_index'])
             if info.get('playlist_index') else None),
        ]:
            if val:
                cmd.extend(['-metadata', f'{key}={val}'])

        cmd.append(str(dst))

        self.to_screen(f'Creating {dst.name}')
        try:
            _subprocess.run(cmd, check=True, capture_output=True)
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
        "format": "399+140/299+140/137+140/298+140/136+140/135+140/134+140/133+140/160+140/best",
        "merge_output_format": "mkv",
        "keepvideo": True,

        # --- "All data" sidecars ---
        "writeinfojson": True,
        "writedescription": True,
        "writethumbnail": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
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
        "outtmpl": "%(id)s/%(uploader)s.%(playlist_title,channel)s.%(upload_date)s - %(title)s [%(id)s].%(ext)s",
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
        ydl.add_post_processor(AudioMetadataPostProcessor(), when='post_process')
        return ydl.download([target_url])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download YouTube video/playlist using yt-dlp Python module with a fixed profile."
    )
    ap.add_argument("target", help="YouTube URL or ID (video id or playlist id).")
    ap.add_argument("--archive", default="archive.txt", help="Download archive file (default: archive.txt).")
    ap.add_argument("--merged-dir", default="merged", help='Output base directory for "home" path (default: merged).')
    ap.add_argument("--cookies-browser", default="firefox", help='Enable cookies-from-browser (default: "firefox"). Pass "" to disable.')
    ap.add_argument("--no-js", action="store_true", help="Disable js_runtimes config.")
    ap.add_argument("--quiet", action="store_true", help="Suppress yt-dlp output.")
    args = ap.parse_args()

    print(f"Target: {to_youtube_url(args.target)}")
    print(f"Output base: {Path(args.merged_dir).expanduser().resolve()}")
    print(f"Archive: {Path(args.archive).expanduser().resolve()}")

    return download_youtube(
        args.target,
        merged_dir=args.merged_dir,
        archive=args.archive,
        cookies_browser=args.cookies_browser,
        no_js=args.no_js,
        quiet=args.quiet,
    )


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
