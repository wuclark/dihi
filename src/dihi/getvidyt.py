#!/usr/bin/env python3
import argparse
import re
from pathlib import Path
import math
from typing import Optional, Dict, Any, Union

from yt_dlp import YoutubeDL

YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAUSIBLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


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
        "retries": math.inf,           # Python API expects numeric, not "infinite"
        "fragment_retries": math.inf,
        "download_archive": str(archive_path),

        # --- Delay between downloads ---
        "sleep_interval": 5,
        "max_sleep_interval": 12,

        # --- Cookies / UA ---
        # Enable cookies-from-browser if provided
        # (example: "firefox", or ("firefox", {"profile": "default-release"}) depending on your setup)
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",

        # --- Format / merge ---
        "format": "399+140/299+140/137+140/298+140/136+140/135+140/134+140/133+140/160+140",
        "merge_output_format": "mkv",
        "keepvideo": True,

        # --- “All data” sidecars ---
        "writeinfojson": True,
        "writedescription": True,
        "writethumbnail": True,
        "convertthumbnails": "png",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "embedsubtitles": True,
        "embedthumbnail": True,
        "embedchapters": True,
        "addmetadata": True,
        "writeplaylistmetafiles": True,

        # --- Output paths / template ---
        "paths": {"home": str(merged_dir)},
        #"outtmpl": "[%(id)s].%(uploader)s.%(playlist_title,channel)s/%(upload_date)s - %(title)s [%(id)s].%(ext)s",
        "outtmpl": "%(id)s/%(uploader)s.%(playlist_title,channel)s.%(upload_date)s - %(title)s [%(id)s].%(ext)s",
    }

    if cookies_browser:
        # In yt-dlp Python API, cookiesfrombrowser can be:
        #   "firefox"
        # or ("firefox", {"profile": "default-release"})
        ydl_opts["cookiesfrombrowser"] = cookies_browser

    if not no_js:
        # Your yt-dlp build expects dict {runtime: {config}}
        ydl_opts["js_runtimes"] = {"node": {}}

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
        return ydl.download([target_url])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download YouTube video/playlist using yt-dlp Python module with a fixed profile."
    )
    ap.add_argument("target", help="YouTube URL or ID (video id or playlist id).")
    ap.add_argument("--archive", default="archive.txt", help="Download archive file (default: archive.txt).")
    ap.add_argument("--merged-dir", default="merged", help='Output base directory for "home" path (default: merged).')
    ap.add_argument("--cookies-browser", default=None, help='Enable cookies-from-browser (e.g. "firefox").')
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
