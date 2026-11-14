"""YouTube access via yt-dlp: search, resolve audio stream URL, convert to mp3."""

import asyncio
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL

from .config import settings

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_valid_id(video_id: str) -> bool:
    return bool(VIDEO_ID_RE.match(video_id))


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _js_runtimes() -> dict:
    # YouTube needs a JS runtime to unlock formats (e.g. 360p video+sound).
    # Solver script comes from the yt-dlp-ejs package.
    for name in ("deno", "node", "bun"):
        if path := shutil.which(name):
            return {name: {"path": path}}
    return {}


def _cookie_file() -> str | None:
    if not settings.yt_cookies.strip():
        return None
    path = Path(tempfile.gettempdir()) / "zoyra_yt_cookies.txt"
    # Env vars often lose newlines; accept literal "\n" too.
    path.write_text(settings.yt_cookies.replace("\\n", "\n").strip() + "\n")
    path.chmod(0o600)
    return str(path)


_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "noplaylist": True,
    "js_runtimes": _js_runtimes(),
}
if cookie_path := _cookie_file():
    _BASE_OPTS["cookiefile"] = cookie_path


# ---------- search ----------

def _search_sync(query: str, limit: int) -> list[dict]:
    opts = {**_BASE_OPTS, "extract_flat": "in_playlist", "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    results = []
    for e in info.get("entries") or []:
        vid = e.get("id")
        if not vid or not is_valid_id(vid):
            continue
        results.append(
            {
                "id": vid,
                "title": e.get("title"),
                "artist": e.get("channel") or e.get("uploader"),
                "duration": e.get("duration"),  # seconds, may be None
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "views": e.get("view_count"),
            }
        )
    return results


async def search(query: str, limit: int) -> list[dict]:
    return await asyncio.to_thread(_search_sync, query, limit)


# ---------- stream URL resolve (cached) ----------

@dataclass
class AudioSource:
    url: str
    headers: dict
    mime: str
    title: str | None
    artist: str | None
    duration: int | None
    expires_at: float


_cache: dict[tuple[str, str], AudioSource] = {}
_resolve_locks: dict[tuple[str, str], asyncio.Lock] = {}

# audio: prefer m4a (AAC) — plays natively in every browser incl. Safari/iOS.
# video: single file with picture + sound (YouTube caps these at 360p; format 18).
_FORMATS = {
    "audio": "bestaudio[ext=m4a]/bestaudio/best",
    "video": "18/best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]",
}


def _mime_for(ext: str | None, kind: str) -> str:
    if kind == "video":
        return {"mp4": "video/mp4", "webm": "video/webm"}.get(ext or "", "video/mp4")
    return {"m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm", "mp3": "audio/mpeg"}.get(
        ext or "", "application/octet-stream"
    )


def _resolve_sync(video_id: str, kind: str) -> AudioSource:
    opts = {**_BASE_OPTS, "format": _FORMATS[kind]}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_watch_url(video_id), download=False)
    # YouTube URLs return 403 until `available_at` (usually ~4s after resolve).
    if (wait := (info.get("available_at") or 0) - time.time()) > 0:
        time.sleep(min(wait + 0.3, 15))
    return AudioSource(
        url=info["url"],
        headers=dict(info.get("http_headers") or {}),
        mime=_mime_for(info.get("ext"), kind),
        title=info.get("title"),
        artist=info.get("artist") or info.get("channel") or info.get("uploader"),
        duration=info.get("duration"),
        expires_at=time.time() + settings.stream_url_ttl,
    )


async def resolve(video_id: str, kind: str = "audio", bad: AudioSource | None = None) -> AudioSource:
    """Cached URL lookup. Pass `bad` (a source that got 403) to force a refresh;
    concurrent callers with the same bad source share one refresh."""
    key = (kind, video_id)
    async with _resolve_locks.setdefault(key, asyncio.Lock()):
        src = _cache.get(key)
        if src and src.expires_at > time.time() and src is not bad:
            return src
        src = await asyncio.to_thread(_resolve_sync, video_id, kind)
        _cache[key] = src
        return src


# ---------- mp3 conversion (cached on disk) ----------

_dl_locks: dict[str, asyncio.Lock] = {}


def _convert_sync(video_id: str, out_dir: Path) -> Path:
    opts = {
        **_BASE_OPTS,
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / f"{video_id}.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(settings.mp3_bitrate),
            },
            {"key": "FFmpegMetadata"},
        ],
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([_watch_url(video_id)])
    return out_dir / f"{video_id}.mp3"


async def get_mp3(video_id: str) -> Path:
    out_dir = settings.download_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{video_id}.mp3"
    if target.exists():
        return target

    lock = _dl_locks.setdefault(video_id, asyncio.Lock())
    async with lock:  # avoid converting same song twice concurrently
        if not target.exists():
            await asyncio.to_thread(_convert_sync, video_id, out_dir)
    return target
