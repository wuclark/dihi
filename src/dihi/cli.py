#!/usr/bin/env python3
"""
dihi — YouTube archive CLI

Commands
--------
  dihi download <target>         Download a video or playlist
  dihi audio-meta <path>         Embed metadata into already-downloaded audio sidecars
  dihi check <target>            Check whether a video is in the local archive (no server needed)

Examples
--------
  dihi download dQw4w9WgXcQ
  dihi download https://www.youtube.com/watch?v=dQw4w9WgXcQ
  dihi download PLxxxxxxxxxxxxxx               # full playlist
  dihi download dQw4w9WgXcQ --audio-meta       # also create clean audio copies
  dihi check dQw4w9WgXcQ
  dihi check dQw4w9WgXcQ --archive ./data/archive.txt
  dihi audio-meta ./data/merged/
  dihi audio-meta ./data/merged/UCxxx/dQw4w9WgXcQ/video.info.json
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

# getvidyt lives alongside cli.py in src/dihi/. When run as an installed entry
# point the package root (src/) is on sys.path but the inner directory isn't,
# so we add it explicitly — matching the flat-module layout Docker provides.
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import getvidyt
from getvidyt import (
    YOUTUBE_VIDEO_ID_RE,
    StandaloneAudioMetaPP,
    download_youtube,
    load_archive,
    to_youtube_url,
)


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def _cmd_download(args) -> int:
    target_url = to_youtube_url(args.target)
    print(f"Target : {target_url}")
    print(f"Output : {Path(args.merged_dir).expanduser().resolve()}")
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


def _add_download_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="YouTube URL, video ID (11 chars), or playlist ID.")
    p.add_argument(
        "--archive",
        default="archive.txt",
        help="yt-dlp download archive file (default: archive.txt).",
    )
    p.add_argument(
        "--merged-dir",
        default="merged",
        help="Output base directory (default: merged).",
    )
    p.add_argument(
        "--cookies-browser",
        default="",
        help='Load cookies from a browser profile, e.g. "firefox". Disabled by default.',
    )
    p.add_argument("--no-js", action="store_true", help="Disable Deno/JS runtime config.")
    p.add_argument("--quiet", action="store_true", help="Suppress yt-dlp output.")
    p.add_argument(
        "--audio-meta",
        action="store_true",
        help="After downloading, create clean audio copies with embedded metadata.",
    )


# ---------------------------------------------------------------------------
# audio-meta
# ---------------------------------------------------------------------------

def _cmd_audio_meta(args) -> int:
    target = Path(args.path).expanduser().resolve()

    if target.is_file() and target.name.endswith(".info.json"):
        ok = StandaloneAudioMetaPP.process_single(target)
        return 0 if ok else 1
    elif target.is_dir():
        count = StandaloneAudioMetaPP.process_directory(
            target, recursive=not args.no_recursive
        )
        return 0 if count > 0 else 1
    else:
        print(f"Error: {target} is not a directory or .info.json file", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def _extract_video_id(raw: str) -> Optional[str]:
    """Pull an 11-char video ID out of a URL or return the raw string if it is already one."""
    raw = raw.strip()
    if YOUTUBE_VIDEO_ID_RE.match(raw):
        return raw
    try:
        parsed = urllib.parse.urlparse(raw)
        vid = urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
        if vid and YOUTUBE_VIDEO_ID_RE.match(vid):
            return vid
    except Exception:
        pass
    return None


def _cmd_check(args) -> int:
    archive_path = Path(args.archive).expanduser().resolve()
    ids = load_archive(archive_path)

    vid = _extract_video_id(args.target)

    if vid:
        found = vid in ids
        status = "FOUND    " if found else "NOT FOUND"
        print(f"{status}  {vid}  ({archive_path})")
        return 0 if found else 1

    # Not a recognised video ID or URL — report archive summary
    print(f"Archive: {archive_path}  ({len(ids)} entries)")
    print(f"(Could not extract a video ID from: {args.target!r})")
    return 2


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="dihi",
        description="YouTube archive CLI — download videos/playlists and manage metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # -- download --
    dl = sub.add_parser("download", help="Download a YouTube video or full playlist.")
    _add_download_args(dl)

    # -- audio-meta --
    am = sub.add_parser(
        "audio-meta",
        help="Create clean audio copies with embedded metadata from already-downloaded files.",
    )
    am.add_argument(
        "path",
        help="Directory to scan recursively, or a single .info.json file.",
    )
    am.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not recurse into subdirectories (default: recurse).",
    )

    # -- check --
    ck = sub.add_parser(
        "check",
        help="Check whether a video ID is present in the local archive (no server needed).",
    )
    ck.add_argument("target", help="Video ID, or full YouTube URL.")
    ck.add_argument(
        "--archive",
        default="archive.txt",
        help="yt-dlp archive file to search (default: archive.txt).",
    )

    args = ap.parse_args()

    if args.command == "download":
        return _cmd_download(args)
    elif args.command == "audio-meta":
        return _cmd_audio_meta(args)
    elif args.command == "check":
        return _cmd_check(args)

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
