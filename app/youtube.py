"""YouTube access via yt-dlp: search, resolve audio stream URL, convert to mp3."""

import asyncio
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

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


class _SilentLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


_BASE_OPTS = {
    "quiet": True,
    "logger": _SilentLogger(),  # failover errors are handled; don't spam the log
    "no_warnings": True,
    "noprogress": True,
    "noplaylist": True,
    "js_runtimes": _js_runtimes(),
}
if cookie_path := _cookie_file():
    _BASE_OPTS["cookiefile"] = cookie_path

# ---------- proxies (failover) ----------
# YT_PROXY may hold several proxies separated by commas. YouTube blocks some IPs
# ("confirm you're not a bot"), so we try them in turn and remember the last one that worked.
def _webshare_proxies() -> list[str]:
    key = settings.webshare_api_key.strip()
    if not key:
        return []
    import httpx  # local: only needed here

    out, url = [], "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page_size=100"
    try:
        while url:
            r = httpx.get(url, headers={"Authorization": f"Token {key}"}, timeout=20)
            r.raise_for_status()
            data = r.json()
            out += [
                f"http://{'xhwydhbq'}:{'vz887p9m3fps'}@{p['proxy_address']}:{p['port']}"
                for p in data.get("results", [])
                if p.get("valid")
            ]
            url = data.get("next")
    except Exception as e:  # noqa: BLE001 - run without Webshare rather than crash
        print(f"Webshare proxy list failed: {e}")
    print(f"Loaded {len(out)} Webshare proxies")
    return out


PROXIES: list[str] = [p.strip() for p in settings.yt_proxy.split(",") if p.strip()] + _webshare_proxies()
_good = 0

_BLOCK_HINTS = (
    "not a bot",
    "Unable to download API page",
    "Failed to extract any player response",
    "HTTP Error 429",
    "HTTP Error 403",
    "timed out",
    "ProxyError",
    "Tunnel connection failed",
)


def _blocked(e: Exception) -> bool:
    msg = str(e)
    return any(h in msg for h in _BLOCK_HINTS)


_cooldown_until: dict[str, float] = {}  # proxy -> time it may be tried again
_COOLDOWN = 600


def _with_proxy(fn):
    """Run fn(proxy): last good proxy first, skip recently blocked ones, fail over on blocks."""
    global _good
    if not PROXIES:
        return fn(None)
    now = time.time()
    order = [(_good + k) % len(PROXIES) for k in range(len(PROXIES))]
    # Cooled-down proxies first; blocked ones only as a last resort.
    order.sort(key=lambda i: _cooldown_until.get(PROXIES[i], 0) > now)
    last: Exception | None = None
    for i in order:
        try:
            result = fn(PROXIES[i])
        except DownloadError as e:
            if not _blocked(e):
                raise  # video itself unavailable: other proxies won't help
            _cooldown_until[PROXIES[i]] = time.time() + _COOLDOWN
            last = e
            continue
        _good = i
        _cooldown_until.pop(PROXIES[i], None)
        return result
    raise last  # type: ignore[misc]


def _opts(proxy: str | None, **extra) -> dict:
    opts = {**_BASE_OPTS, **extra}
    if proxy:
        opts["proxy"] = proxy
    return opts


# ---------- search ----------

def _search_sync(query: str, limit: int) -> list[dict]:
    def run(proxy):
        with YoutubeDL(_opts(proxy, extract_flat="in_playlist", skip_download=True)) as ydl:
            return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    info = _with_proxy(run)

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
    proxy: str | None = None  # stream URLs only work from the IP that resolved them


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
    def run(proxy):
        with YoutubeDL(_opts(proxy, skip_download=True, ignore_no_formats_error=True)) as ydl:
            return ydl.extract_info(_watch_url(video_id), download=False, process=False)

    return _store_meta(video_id, _with_proxy(run))


async def get_meta(video_id: str) -> dict:
    if meta := _meta.get(video_id):
        return meta
    return await asyncio.to_thread(_meta_sync, video_id)


def _resolve_sync(video_id: str, kind: str) -> AudioSource:
    def run(proxy):
        with YoutubeDL(_opts(proxy, format=_FORMATS[kind])) as ydl:
            return proxy, ydl.extract_info(_watch_url(video_id), download=False)

    proxy, info = _with_proxy(run)
    _store_meta(video_id, info)
    return AudioSource(
        proxy=proxy,
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

    def run(proxy):
        with YoutubeDL(_opts(proxy, **opts)) as ydl:
            ydl.download([_watch_url(video_id)])

    _with_proxy(run)
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
