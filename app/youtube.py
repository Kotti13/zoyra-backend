
"""YouTube access via yt-dlp with Proxifly proxy rotation."""

import asyncio
import re
import shutil
import tempfile
import threading
import time

from dataclasses import dataclass
from pathlib import Path

import httpx

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import settings


# ============================================================
# BASIC HELPERS
# ============================================================

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_valid_id(video_id: str) -> bool:
    return bool(VIDEO_ID_RE.fullmatch(video_id))


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _js_runtimes() -> dict:
    for name in ("deno", "node", "bun"):
        if path := shutil.which(name):
            return {name: {"path": path}}
    return {}


def _cookie_file() -> str | None:
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


# ============================================================
# YT-DLP OPTIONS
# ============================================================

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


# ============================================================
# PROXIFLY CONFIGURATION
# ============================================================

PROXIFLY_URL = "https://api.proxifly.dev/get-proxy"

PROXIES: list[str] = []

_good = 0

_proxy_lock = threading.RLock()

_cooldown_until: dict[str, float] = {}

_COOLDOWN = 600

# Number of Proxifly proxies to test during initial startup.
_INITIAL_PROXY_ATTEMPTS = 3

# Number of fresh proxies to request after existing proxies fail.
_REFRESH_PROXY_ATTEMPTS = 3


# ============================================================
# PROXIFLY PROXY VALIDATION
# ============================================================

def _test_proxy(proxy: str) -> bool:
    """
    Test whether a proxy can establish an HTTPS connection
    to YouTube.

    This tests actual connectivity instead of trusting
    Proxifly's anonymity or HTTPS metadata.
    """

    try:
        with httpx.Client(
            proxy=proxy,
            timeout=12,
            follow_redirects=False,
            trust_env=False,
        ) as client:

            response = client.get(
                "https://www.youtube.com/generate_204"
            )

            if response.status_code != 204:
                print(
                    f"Proxy rejected: unexpected YouTube response "
                    f"{response.status_code}"
                )
                return False

        return True

    except Exception as e:
        print(f"Proxy HTTPS CONNECT test failed: {e}")
        return False


def _get_proxifly_proxy() -> str | None:
    """Fetch and validate one proxy from Proxifly."""

    try:
        response = httpx.get(
            PROXIFLY_URL,
            timeout=15,
            follow_redirects=True,
            trust_env=False,
        )

        response.raise_for_status()

        data = response.json()

        proxy = data.get("proxy")
        protocol = (data.get("protocol") or "").lower()
        anonymity = (data.get("anonymity") or "").lower()

        if not proxy:
            print("Proxifly returned an empty proxy")
            return None

        if protocol not in ("http", "https", "socks5", "socks5h"):
            print(f"Unsupported proxy protocol: {protocol}")
            return None

        if not proxy.startswith(
            ("http://", "https://", "socks5://", "socks5h://")
        ):
            print("Invalid proxy URL")
            return None

        # Do not use proxies that explicitly identify themselves
        # as transparent.
        if anonymity == "transparent":
            print(
                f"Rejected transparent proxy: {data.get('ip')}"
            )
            return None

        print(
            f"Testing Proxifly proxy: "
            f"{data.get('ip')}:{data.get('port')} "
            f"({protocol}, {anonymity})"
        )

        if not _test_proxy(proxy):
            print("Proxifly proxy failed HTTPS validation")
            return None

        print(
            f"Validated Proxifly proxy: "
            f"{data.get('ip')} ({protocol})"
        )

        return proxy

    except Exception as e:
        print(f"Proxifly API error: {e}")
        return None


def _add_proxy(proxy: str) -> None:
    """Add a validated proxy without duplicates."""

    with _proxy_lock:
        if proxy not in PROXIES:
            PROXIES.append(proxy)
            print(f"Proxy pool size: {len(PROXIES)}")


def _load_proxies() -> list[str]:
    """Load configured proxies and fetch Proxifly proxies."""

    proxies = [
        p.strip()
        for p in settings.yt_proxy.split(",")
        if p.strip()
    ]

    for proxy in proxies:
        if not _test_proxy(proxy):
            print(f"Configured proxy failed validation: {proxy}")

    proxies = [
        p for p in proxies if _test_proxy(p)
    ]

    for _ in range(_INITIAL_PROXY_ATTEMPTS):
        proxy = _get_proxifly_proxy()

        if proxy and proxy not in proxies:
            proxies.append(proxy)

        if len(proxies) >= _INITIAL_PROXY_ATTEMPTS:
            break

    return proxies


PROXIES = _load_proxies()

print(f"Loaded {len(PROXIES)} working proxies")


# ============================================================
# PROXY ERROR DETECTION
# ============================================================

_BLOCK_HINTS = (
    "not a bot",
    "Unable to download API page",
    "Failed to extract any player response",
    "HTTP Error 429",
    "HTTP Error 403",
    "HTTP Error 400",
    "400 Bad Request",
    "timed out",
    "ProxyError",
    "Tunnel connection failed",
    "Connection refused",
    "Connection reset",
    "Proxy connection failed",
    "Unable to connect to proxy",
)


def _blocked(e: Exception) -> bool:
    msg = str(e).lower()

    return any(
        hint.lower() in msg
        for hint in _BLOCK_HINTS
    )


# ============================================================
# PROXY FAILOVER AND ROTATION
# ============================================================

def _refresh_proxies() -> str | None:
    """Fetch fresh proxies after current proxies fail."""

    for _ in range(_REFRESH_PROXY_ATTEMPTS):
        proxy = _get_proxifly_proxy()

        if not proxy:
            continue

        with _proxy_lock:
            if proxy in PROXIES:
                continue

            PROXIES.append(proxy)

        return proxy

    return None


def _with_proxy(fn):
    """
    Execute a yt-dlp operation using validated proxies.

    Order:
    1. Previously working proxy.
    2. Other available proxies.
    3. Fresh Proxifly proxies.
    4. Direct connection only if no proxy was available.
    """

    global _good

    with _proxy_lock:
        proxies = list(PROXIES)
        good = _good

    if not proxies:
        print("No validated proxies available; trying direct connection")
        return fn(None)

    now = time.time()

    order = [
        (good + k) % len(proxies)
        for k in range(len(proxies))
    ]

    # Prefer proxies outside cooldown.
    order.sort(
        key=lambda i: (
            _cooldown_until.get(proxies[i], 0) > now
        )
    )

    attempted = set()
    last_error: Exception | None = None

    def attempt(proxy: str):
        nonlocal last_error

        attempted.add(proxy)

        try:
            result = fn(proxy)

        except DownloadError as e:
            if not _blocked(e):
                raise

            print(f"Proxy failed: {proxy}")

            with _proxy_lock:
                _cooldown_until[proxy] = (
                    time.time() + _COOLDOWN
                )

            last_error = e
            return False, None

        with _proxy_lock:
            if proxy in PROXIES:
                _good = PROXIES.index(proxy)

            _cooldown_until.pop(proxy, None)

        print(f"Proxy request successful: {proxy}")

        return True, result

    # Try existing proxies.
    for i in order:
        proxy = proxies[i]

        if _cooldown_until.get(proxy, 0) > time.time():
            continue

        success, result = attempt(proxy)

        if success:
            return result

    # Fetch fresh proxies if existing proxies failed.
    print("Existing proxies failed. Refreshing Proxifly...")

    for _ in range(_REFRESH_PROXY_ATTEMPTS):
        proxy = _refresh_proxies()

        if not proxy or proxy in attempted:
            continue

        success, result = attempt(proxy)

        if success:
            return result

    if last_error:
        raise last_error

    raise RuntimeError(
        "No working Proxifly proxy available"
    )


# ============================================================
# YT-DLP OPTIONS WITH PROXY
# ============================================================

def _opts(proxy: str | None, **extra) -> dict:
    opts = {
        **_BASE_OPTS,
        **extra,
    }

    if proxy:
        opts["proxy"] = proxy

    return opts


# ============================================================
# YOUTUBE SEARCH
# ============================================================

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

    for entry in info.get("entries") or []:

        video_id = entry.get("id")

        if not video_id or not is_valid_id(video_id):
            continue

        results.append(
            {
                "id": video_id,
                "title": entry.get("title"),
                "artist": (
                    entry.get("channel")
                    or entry.get("uploader")
                ),
                "duration": entry.get("duration"),
                "thumbnail": (
                    f"https://i.ytimg.com/vi/"
                    f"{video_id}/hqdefault.jpg"
                ),
                "views": entry.get("view_count"),
            }
        )

    return results


async def search(query: str, limit: int) -> list[dict]:
    return await asyncio.to_thread(
        _search_sync,
        query,
        limit,
    )


# ============================================================
# AUDIO SOURCE
# ============================================================

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


# ============================================================
# FORMAT SELECTION
# ============================================================

_FORMATS = {
    "audio": "bestaudio[ext=m4a]/bestaudio/best",

    "video": (
        "18/"
        "best[ext=mp4][vcodec!=none][acodec!=none]/"
        "best[vcodec!=none][acodec!=none]"
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


# ============================================================
# SONG METADATA
# ============================================================

_meta: dict[str, dict] = {}


def _store_meta(video_id: str, info: dict) -> dict:

    artists = info.get("artists") or []

    meta = {
        "title": info.get("title"),
        "track": info.get("track"),
        "artist": info.get("artist") or (
            artists[0] if artists else None
        ),
        "album": info.get("album"),
        "channel": (
            info.get("channel")
            or info.get("uploader")
        ),
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


# ============================================================
# AUDIO / VIDEO RESOLUTION
# ============================================================

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

        headers=dict(
            info.get("http_headers") or {}
        ),

        mime=_mime_for(
            info.get("ext"),
            kind,
        ),

        title=info.get("title"),

        artist=(
            info.get("artist")
            or info.get("channel")
            or info.get("uploader")
        ),

        duration=info.get("duration"),

        expires_at=(
            time.time() + settings.stream_url_ttl
        ),
    )


async def resolve(
    video_id: str,
    kind: str = "audio",
    bad: AudioSource | None = None,
) -> AudioSource:
    """
    Resolve a cached audio/video URL.

    Pass bad when the previous source returned HTTP 403
    to force a fresh resolution.
    """

    key = (kind, video_id)

    async with _resolve_locks.setdefault(
        key,
        asyncio.Lock(),
    ):

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


# ============================================================
# BACKGROUND PREFETCH
# ============================================================

_bg_tasks: set[asyncio.Task] = set()

_prefetch_slots: asyncio.Semaphore | None = None


def prefetch(
    video_id: str,
    kind: str = "audio",
) -> None:
    """Resolve a stream URL in the background."""

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

        except Exception as e:
            print(f"Prefetch failed for {video_id}: {e}")

    task = asyncio.create_task(run())

    _bg_tasks.add(task)

    task.add_done_callback(_bg_tasks.discard)


# ============================================================
# MP3 CONVERSION
# ============================================================

_dl_locks: dict[str, asyncio.Lock] = {}


def _convert_sync(
    video_id: str,
    out_dir: Path,
) -> Path:

    opts = {
        "format": "bestaudio/best",

        "outtmpl": str(
            out_dir / f"{video_id}.%(ext)s"
        ),

        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(
                    settings.mp3_bitrate
                ),
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
