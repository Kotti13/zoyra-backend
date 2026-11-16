
"""YouTube access via yt-dlp with Proxifly proxy rotation."""

import asyncio
import re
import shutil
import tempfile
import time
import threading

from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import settings


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# ---------- basic helpers ----------


def is_valid_id(video_id: str) -> bool:
    return bool(VIDEO_ID_RE.match(video_id))


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _js_runtimes() -> dict:
    """Find an available JavaScript runtime for yt-dlp."""

    for name in ("deno", "node", "bun"):
        if path := shutil.which(name):
            return {name: {"path": path}}

    return {}


def _cookie_file() -> str | None:
    """Create a temporary YouTube cookies file."""

    if not settings.yt_cookies.strip():
        return None

    path = Path(tempfile.gettempdir()) / "zoyra_yt_cookies.txt"

    path.write_text(
        settings.yt_cookies.replace("\\n", "\n").strip() + "\n"
    )

    path.chmod(0o600)

    return str(path)


class _SilentLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


# ---------- base yt-dlp options ----------

_BASE_OPTS = {
    "quiet": True,
    "logger": _SilentLogger(),
    "no_warnings": True,
    "noprogress": True,
    "noplaylist": True,
    "js_runtimes": _js_runtimes(),
}

if cookie_path := _cookie_file():
    _BASE_OPTS["cookiefile"] = cookie_path


# ---------- Proxifly proxy management ----------

PROXIFLY_URL = "https://api.proxifly.dev/get-proxy"

PROXIES: list[str] = []

_good = 0

_proxy_lock = threading.RLock()

_cooldown_until: dict[str, float] = {}

_COOLDOWN = 600


def _get_proxifly_proxy() -> str | None:
    """
    Fetch a proxy from Proxifly.

    Supports SOCKS5, HTTP and HTTPS proxy URLs.
    """

    import httpx

    try:
        response = httpx.get(
            PROXIFLY_URL,
            timeout=15,
            follow_redirects=True,
        )

        response.raise_for_status()

        data = response.json()

        proxy = data.get("proxy")

        if not proxy:
            print("Proxifly returned an empty proxy")
            return None

        protocol = data.get("protocol", "").lower()

        if protocol not in ("socks5", "socks5h", "http", "https"):
            print(f"Unsupported Proxifly protocol: {protocol}")
            return None

        if not proxy.startswith(
            ("socks5://", "socks5h://", "http://", "https://")
        ):
            print("Invalid Proxifly proxy URL")
            return None

        anonymity = data.get("anonymity", "unknown")

        if anonymity == "transparent":
            print(
                "WARNING: Proxifly returned a transparent proxy. "
                "It may expose your original IP."
            )

        print(
            f"Proxifly proxy received: "
            f"{data.get('ip')}:{data.get('port')} "
            f"({protocol}, {anonymity})"
        )

        return proxy

    except Exception as e:
        print(f"Proxifly API error: {e}")
        return None


def _load_proxies() -> list[str]:
    """Load environment proxies and fetch one Proxifly proxy."""

    proxies = [
        p.strip()
        for p in settings.yt_proxy.split(",")
        if p.strip()
    ]

    proxifly_proxy = _get_proxifly_proxy()

    if proxifly_proxy and proxifly_proxy not in proxies:
        proxies.append(proxifly_proxy)

    return proxies


def _refresh_proxies() -> str | None:
    """
    Fetch a fresh Proxifly proxy when the current proxy fails.
    """

    global _good

    proxy = _get_proxifly_proxy()

    if not proxy:
        return None

    with _proxy_lock:
        if proxy not in PROXIES:
            PROXIES.append(proxy)
            print(f"Added new proxy. Total proxies: {len(PROXIES)}")

        return proxy


PROXIES = _load_proxies()


# ---------- proxy error detection ----------

_BLOCK_HINTS = (
    "not a bot",
    "Unable to download API page",
    "Failed to extract any player response",
    "HTTP Error 429",
    "HTTP Error 403",
    "timed out",
    "ProxyError",
    "Tunnel connection failed",
    "Connection refused",
    "Connection reset",
    "Proxy connection failed",
)


def _blocked(e: Exception) -> bool:
    msg = str(e)

    return any(h.lower() in msg.lower() for h in _BLOCK_HINTS)


# ---------- proxy failover ----------


def _with_proxy(fn):
    """
    Run fn(proxy).

    1. Try the last working proxy first.
    2. Skip proxies currently in cooldown.
    3. Try remaining proxies.
    4. Fetch a new Proxifly proxy if all fail.
    5. Fall back to direct connection if no proxy is available.
    """

    global _good

    with _proxy_lock:
        proxies = list(PROXIES)
        good = _good

    if not proxies:
        print("No proxies available. Using direct connection.")
        return fn(None)

    now = time.time()

    order = [
        (good + k) % len(proxies)
        for k in range(len(proxies))
    ]

    order.sort(
        key=lambda i: (
            _cooldown_until.get(proxies[i], 0) > now
        )
    )

    attempted = set()
    last: Exception | None = None

    def attempt(proxy_list, indices):
        nonlocal last

        for i in indices:
            proxy = proxy_list[i]

            if proxy in attempted:
                continue

            attempted.add(proxy)

            try:
                result = fn(proxy)

            except DownloadError as e:
                if not _blocked(e):
                    raise

                print(f"Proxy failed: {proxy}")

                _cooldown_until[proxy] = time.time() + _COOLDOWN

                last = e
                continue

            except Exception:
                raise

            with _proxy_lock:
                if proxy in PROXIES:
                    _good = PROXIES.index(proxy)

                _cooldown_until.pop(proxy, None)

            print(f"Proxy working: {proxy}")

            return True, result

        return False, None

    success, result = attempt(proxies, order)

    if success:
        return result

    # All existing proxies failed. Request another proxy.
    print("All proxies failed. Refreshing Proxifly...")

    new_proxy = _refresh_proxies()

    if new_proxy and new_proxy not in attempted:
        try:
            result = fn(new_proxy)

        except DownloadError as e:
            if not _blocked(e):
                raise

            print(f"New Proxifly proxy failed: {new_proxy}")

            _cooldown_until[new_proxy] = time.time() + _COOLDOWN

            last = e

        else:
            with _proxy_lock:
                if new_proxy in PROXIES:
                    _good = PROXIES.index(new_proxy)

                _cooldown_until.pop(new_proxy, None)

            print("New Proxifly proxy working!")

            return result

    if last:
        raise last

    raise RuntimeError("No working proxy available")


def _opts(proxy: str | None, **extra) -> dict:
    """Build yt-dlp options with optional proxy."""

    opts = {
        **_BASE_OPTS,
        **extra,
    }

    if proxy:
        opts["proxy"] = proxy

    return opts


# ---------- search ----------


def _search_sync(query: str, limit: int) -> list[dict]:

    def run(proxy):
        with YoutubeDL(
            _opts(
                proxy,
                extract_flat="in_playlist",
                skip_download=True,
            )
        ) as ydl:

            return ydl.extract_info(
                f"ytsearch{limit}:{query}",
                download=False,
            )

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
                "duration": e.get("duration"),
                "thumbnail": (
                    f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                ),
                "views": e.get("view_count"),
            }
        )

    return results


async def search(query: str, limit: int) -> list[dict]:
    return await asyncio.to_thread(
        _search_sync,
        query,
        limit,
    )


# ---------- stream URL resolve ----------


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
    proxy: str | None = None


_cache: dict[tuple[str, str], AudioSource] = {}

_resolve_locks: dict[tuple[str, str], asyncio.Lock] = {}


_FORMATS = {
    "audio": "bestaudio[ext=m4a]/bestaudio/best",
    "video": (
        "18/best[ext=mp4][vcodec!=none][acodec!=none]"
        "/best[vcodec!=none][acodec!=none]"
    ),
}


def _mime_for(ext: str | None, kind: str) -> str:

    if kind == "video":
        return {
            "mp4": "video/mp4",
            "webm": "video/webm",
        }.get(ext or "", "video/mp4")

    return {
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "webm": "audio/webm",
        "mp3": "audio/mpeg",
    }.get(ext or "", "application/octet-stream")


# ---------- song metadata ----------

_meta: dict[str, dict] = {}


def _store_meta(video_id: str, info: dict) -> dict:

    meta = {
        "title": info.get("title"),
        "track": info.get("track"),
        "artist": (
            info.get("artist")
            or (info.get("artists") or [None])[0]
        ),
        "album": info.get("album"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
    }

    _meta[video_id] = meta

    return meta


def _meta_sync(video_id: str) -> dict:

    def run(proxy):
        with YoutubeDL(
            _opts(
                proxy,
                skip_download=True,
                ignore_no_formats_error=True,
            )
        ) as ydl:

            return ydl.extract_info(
                _watch_url(video_id),
                download=False,
                process=False,
            )

    return _store_meta(
        video_id,
        _with_proxy(run),
    )


async def get_meta(video_id: str) -> dict:

    if meta := _meta.get(video_id):
        return meta

    return await asyncio.to_thread(
        _meta_sync,
        video_id,
    )


# ---------- resolve audio/video ----------


def _resolve_sync(video_id: str, kind: str) -> AudioSource:

    def run(proxy):

        with YoutubeDL(
            _opts(
                proxy,
                format=_FORMATS[kind],
            )
        ) as ydl:

            info = ydl.extract_info(
                _watch_url(video_id),
                download=False,
            )

            return proxy, info

    proxy, info = _with_proxy(run)

    _store_meta(video_id, info)

    return AudioSource(
        proxy=proxy,
        available_at=min(
            float(info.get("available_at") or 0) + 0.3,
            time.time() + 15,
        ),
        url=info["url"],
        headers=dict(info.get("http_headers") or {}),
        mime=_mime_for(info.get("ext"), kind),
        title=info.get("title"),
        artist=(
            info.get("artist")
            or info.get("channel")
            or info.get("uploader")
        ),
        duration=info.get("duration"),
        expires_at=time.time() + settings.stream_url_ttl,
    )


async def resolve(
    video_id: str,
    kind: str = "audio",
    bad: AudioSource | None = None,
) -> AudioSource:
    """
    Resolve and cache a YouTube stream URL.

    Pass bad when the previous source returned HTTP 403.
    """

    key = (kind, video_id)

    async with _resolve_locks.setdefault(key, asyncio.Lock()):

        src = _cache.get(key)

        if not (
            src
            and src.expires_at > time.time()
            and src is not bad
        ):

            src = await asyncio.to_thread(
                _resolve_sync,
                video_id,
                kind,
            )

            _cache[key] = src

        wait = src.available_at - time.time()

        if wait > 0:
            await asyncio.sleep(wait)

        return src


# ---------- prefetch ----------


_bg_tasks: set[asyncio.Task] = set()

_prefetch_slots: asyncio.Semaphore | None = None


def prefetch(
    video_id: str,
    kind: str = "audio",
) -> None:

    """Resolve stream URLs in the background."""

    src = _cache.get((kind, video_id))

    if src and src.expires_at > time.time() + 60:
        return

    global _prefetch_slots

    if _prefetch_slots is None:
        _prefetch_slots = asyncio.Semaphore(
            settings.prefetch_concurrency
        )

    async def run():

        try:
            async with _prefetch_slots:
                await resolve(video_id, kind)

        except Exception:
            pass

    task = asyncio.create_task(run())

    _bg_tasks.add(task)

    task.add_done_callback(_bg_tasks.discard)


# ---------- MP3 conversion ----------


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
            {
                "key": "FFmpegMetadata",
            },
        ],
    }

    def run(proxy):

        with YoutubeDL(
            _opts(
                proxy,
                **opts,
            )
        ) as ydl:

            ydl.download(
                [_watch_url(video_id)]
            )

    _with_proxy(run)

    return out_dir / f"{video_id}.mp3"


async def get_mp3(video_id: str) -> Path:

    out_dir = settings.download_dir

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    target = out_dir / f"{video_id}.mp3"

    if target.exists():
        return target

    lock = _dl_locks.setdefault(
        video_id,
        asyncio.Lock(),
    )

    async with lock:

        if not target.exists():

            await asyncio.to_thread(
                _convert_sync,
                video_id,
                out_dir,
            )

    return target
