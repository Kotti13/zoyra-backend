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
if settings.yt_proxy.strip():
    _BASE_OPTS["proxy"] = settings.yt_proxy.strip()


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
    available_at: float = 0.0


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


# Song metadata, filled by every resolve. Used by lyrics lookup.
_meta: dict[str, dict] = {}


def _store_meta(video_id: str, info: dict) -> dict:
    meta = {
        "title": info.get("title"),
        "track": info.get("track"),  # set for YouTube Music / auto-generated uploads
        "artist": info.get("artist") or (info.get("artists") or [None])[0],
        "album": info.get("album"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
    }
    _meta[video_id] = meta
    return meta


def _meta_sync(video_id: str) -> dict:
    # No format selection / availability wait needed — metadata only.
    opts = {**_BASE_OPTS, "skip_download": True, "ignore_no_formats_error": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_watch_url(video_id), download=False, process=False)
    return _store_meta(video_id, info)


async def get_meta(video_id: str) -> dict:
    if meta := _meta.get(video_id):
        return meta
    return await asyncio.to_thread(_meta_sync, video_id)


def _resolve_sync(video_id: str, kind: str) -> AudioSource:
    opts = {**_BASE_OPTS, "format": _FORMATS[kind]}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_watch_url(video_id), download=False)
    _store_meta(video_id, info)
    return AudioSource(
        # YouTube URLs return 403 until `available_at` (usually ~4s after resolve);
        # callers wait for it asynchronously so worker threads aren't held.
        available_at=min(float(info.get("available_at") or 0) + 0.3, time.time() + 15),
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
        if not (src and src.expires_at > time.time() and src is not bad):
            src = await asyncio.to_thread(_resolve_sync, video_id, kind)
            _cache[key] = src
    # Prefetched moments ago? Only wait out what's left of YouTube's delay.
    if (wait := src.available_at - time.time()) > 0:
        await asyncio.sleep(wait)
    return src


# ---------- prefetch (warm the cache before the user taps play) ----------

_bg_tasks: set[asyncio.Task] = set()
_prefetch_slots: asyncio.Semaphore | None = None  # created lazily inside the event loop


def prefetch(video_id: str, kind: str = "audio") -> None:
    """Resolve in the background; no-op if already cached. Errors are ignored."""
    src = _cache.get((kind, video_id))
    if src and src.expires_at > time.time() + 60:
        return

    global _prefetch_slots
    if _prefetch_slots is None:
        _prefetch_slots = asyncio.Semaphore(settings.prefetch_concurrency)

    async def run():
        try:
            async with _prefetch_slots:  # typing in search fires many queries; cap background lookups
                await resolve(video_id, kind)
        except Exception:  # noqa: BLE001 - best effort
            pass

    task = asyncio.create_task(run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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
