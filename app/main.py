
import asyncio
import re
import secrets

from contextlib import asynccontextmanager

import httpx

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)

from yt_dlp.utils import DownloadError

from . import lyrics, youtube
from .config import settings


# ============================================================
# APPLICATION LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    timeout = httpx.Timeout(
        30.0,
        read=None,
    )

    # General HTTP client for lyrics and other services.
    app.state.http = httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
    )

    # YouTube media clients.
    # None represents a direct connection.
    app.state.yt_http = {
        None: app.state.http
    }

    # Protect dynamic client creation.
    app.state.yt_http_lock = asyncio.Lock()

    yield

    # Close all dynamically created proxy clients.
    for proxy, client in list(app.state.yt_http.items()):

        if proxy is not None:
            await client.aclose()

    # Close the general client.
    await app.state.http.aclose()


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="Zoyra Music API",
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================
# DYNAMIC YOUTUBE HTTP CLIENT
# ============================================================

async def _get_yt_client(
    request: Request,
    proxy: str | None,
) -> httpx.AsyncClient:
    """
    Get or create an HTTP client for a YouTube proxy.

    A proxy may be fetched after application startup.
    Therefore, clients must be created dynamically.
    """

    clients = request.app.state.yt_http

    if proxy is None:
        return clients[None]

    client_proxy = proxy

    # HTTPX SOCKS transport uses socks5://.
    if client_proxy.startswith("socks5h://"):
        client_proxy = client_proxy.replace(
            "socks5h://",
            "socks5://",
            1,
        )

    async with request.app.state.yt_http_lock:

        client = clients.get(proxy)

        if client is None:

            timeout = httpx.Timeout(
                30.0,
                read=None,
            )

            client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                proxy=client_proxy,
                trust_env=False,
            )

            clients[proxy] = client

            print(
                "Created YouTube streaming client "
                "for selected proxy"
            )

        return client


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
    ],
)


# ============================================================
# API KEY MIDDLEWARE
# ============================================================

@app.middleware("http")
async def require_api_key(
    request: Request,
    call_next,
):

    key = settings.api_key.strip()
    path = request.url.path

    if (
        key
        and path.startswith("/api/")
        and path != "/api/health"
        and request.method != "OPTIONS"
    ):

        sent = (
            request.headers.get("x-zoyra-key")
            or request.query_params.get("key")
            or ""
        )

        if not secrets.compare_digest(
            sent.encode(),
            key.encode(),
        ):

            return JSONResponse(
                {
                    "detail": "Invalid or missing API key"
                },
                status_code=401,
            )

    return await call_next(request)


# ============================================================
# BASIC HELPERS
# ============================================================

def _check_id(video_id: str) -> None:

    if not youtube.is_valid_id(video_id):
        raise HTTPException(
            400,
            "Invalid video id",
        )


def _audio_links(
    request: Request,
    video_id: str,
) -> dict:

    base = str(request.base_url).rstrip("/")

    # Players load these URLs directly.
    # Carry the API key along when one is configured.
    q = (
        f"?key={settings.api_key.strip()}"
        if settings.api_key.strip()
        else ""
    )

    return {
        "stream_url": (
            f"{base}/api/stream/{video_id}{q}"
        ),
        "video_url": (
            f"{base}/api/video/{video_id}{q}"
        ),
        "download_url": (
            f"{base}/api/download/{video_id}{q}"
        ),
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/api/health")
async def health():

    return {
        "status": "ok"
    }


# ============================================================
# SEARCH
# ============================================================

@app.get("/api/search")
async def search(
    request: Request,
    q: str = Query(
        ...,
        min_length=1,
        max_length=200,
        description="Song, artist or album",
    ),
    limit: int = Query(
        10,
        ge=1,
    ),
):

    limit = min(
        limit,
        settings.max_search_results,
    )

    try:

        results = await youtube.search(
            q.strip(),
            limit,
        )

    except DownloadError as e:

        raise HTTPException(
            502,
            f"Search failed: {e}",
        ) from e

    for result in results:

        result.update(
            _audio_links(
                request,
                result["id"],
            )
        )

    # Prefetch the first few results.
    for result in results[:settings.prefetch_count]:

        youtube.prefetch(
            result["id"]
        )

    return {
        "query": q,
        "count": len(results),
        "results": results,
    }


# ============================================================
# BACKGROUND PREFETCH
# ============================================================

@app.post(
    "/api/prefetch/{video_id}",
    status_code=202,
)
async def prefetch(
    video_id: str,
    kind: str = Query(
        "audio",
        pattern="^(audio|video)$",
    ),
):

    """
    Warm the stream URL in the background.
    """

    _check_id(video_id)

    youtube.prefetch(
        video_id,
        kind,
    )

    return {
        "id": video_id,
        "queued": True,
    }


# ============================================================
# SONG INFORMATION
# ============================================================

@app.get("/api/song/{video_id}")
async def song_info(
    video_id: str,
    request: Request,
):

    _check_id(video_id)

    try:

        src = await youtube.resolve(
            video_id
        )

    except DownloadError as e:

        raise HTTPException(
            404,
            f"Song unavailable: {e}",
        ) from e

    return {
        "id": video_id,
        "title": src.title,
        "artist": src.artist,
        "duration": src.duration,
        "thumbnail": (
            f"https://i.ytimg.com/vi/"
            f"{video_id}/hqdefault.jpg"
        ),
        **_audio_links(
            request,
            video_id,
        ),
    }


# ============================================================
# AUDIO STREAM
# ============================================================

@app.get("/api/stream/{video_id}")
async def stream(
    video_id: str,
    request: Request,
):

    """
    Proxy audio to the browser or mobile player.

    Supports HTTP Range requests for seeking.
    """

    return await _proxy(
        video_id,
        request,
        "audio",
    )


# ============================================================
# VIDEO STREAM
# ============================================================

@app.get("/api/video/{video_id}")
async def video(
    video_id: str,
    request: Request,
):

    """
    Proxy video (MP4 with sound).

    Supports HTTP Range requests.
    """

    return await _proxy(
        video_id,
        request,
        "video",
    )


# ============================================================
# RANGE REQUEST HELPERS
# ============================================================

_CHUNK = 2 * 1024 * 1024

_RANGE_RE = re.compile(
    r"bytes=(\d*)-(\d*)$"
)


def _parse_range(
    value: str | None,
) -> tuple[int, int | None] | None:

    """
    Parse a single bytes=start-end range.
    """

    if not value:
        return None

    match = _RANGE_RE.match(
        value.strip()
    )

    if not match or not match.group(1):
        return None

    return (
        int(match.group(1)),
        int(match.group(2)) if match.group(2) else None,
    )


# ============================================================
# STREAM PROXY
# ============================================================

async def _proxy(
    video_id: str,
    request: Request,
    kind: str,
):

    _check_id(video_id)

    asked = _parse_range(
        request.headers.get("range")
    )

    start, end = (
        asked
        if asked
        else (0, None)
    )

    # --------------------------------------------------------
    # OPEN UPSTREAM CHUNK
    # --------------------------------------------------------

    async def open_chunk(
        src,
        first: int,
        last: int,
    ):

        headers = {
            **src.headers,
            "Range": f"bytes={first}-{last}",
        }

        # Dynamically get the client for src.proxy.
        client = await _get_yt_client(
            request,
            src.proxy,
        )

        upstream_request = client.build_request(
            "GET",
            src.url,
            headers=headers,
        )

        return await client.send(
            upstream_request,
            stream=True,
        )

    # --------------------------------------------------------
    # RESOLVE AND OPEN STREAM
    # --------------------------------------------------------

    first_last = (
        start + _CHUNK - 1
        if end is None
        else min(
            end,
            start + _CHUNK - 1,
        )
    )

    try:

        src = await youtube.resolve(
            video_id,
            kind,
        )

        upstream = await open_chunk(
            src,
            start,
            first_last,
        )

        # Cached URL expired.
        # Re-resolve once.
        if upstream.status_code == 403:

            await upstream.aclose()

            src = await youtube.resolve(
                video_id,
                kind,
                bad=src,
            )

            upstream = await open_chunk(
                src,
                start,
                first_last,
            )

    except DownloadError as e:

        raise HTTPException(
            404,
            f"Song unavailable: {e}",
        ) from e

    except httpx.HTTPError as e:

        raise HTTPException(
            502,
            f"Upstream error: {e}",
        ) from e

    except Exception as e:

        print(
            f"Stream initialization error: "
            f"{type(e).__name__}: {e}"
        )

        raise HTTPException(
            502,
            "Unable to initialize audio stream",
        ) from e

    # --------------------------------------------------------
    # UPSTREAM STATUS
    # --------------------------------------------------------

    if upstream.status_code == 416:

        total = (
            upstream.headers.get(
                "content-range",
                "",
            ).rpartition("/")[2]
            or "*"
        )

        await upstream.aclose()

        return Response(
            status_code=416,
            headers={
                "content-range": f"bytes */{total}"
            },
        )

    if upstream.status_code not in (200, 206):

        status = upstream.status_code

        await upstream.aclose()

        print(
            f"YouTube upstream returned HTTP {status}"
        )

        raise HTTPException(
            502,
            f"Upstream returned {status}",
        )

    # --------------------------------------------------------
    # TOTAL FILE SIZE
    # --------------------------------------------------------

    content_range = upstream.headers.get(
        "content-range",
        "",
    )

    total = (
        int(content_range.rpartition("/")[2])
        if (
            "/" in content_range
            and content_range.rpartition("/")[2].isdigit()
        )
        else None
    )

    if total is None:

        total = (
            int(
                upstream.headers.get(
                    "content-length",
                    0,
                )
            )
            or None
        )

    last = (
        total - 1
        if end is None and total
        else (
            min(end, total - 1)
            if end is not None and total
            else end
        )
    )

    # --------------------------------------------------------
    # STREAM RESPONSE BODY
    # --------------------------------------------------------

    async def body():

        resp = upstream
        pos = start

        try:

            while True:

                async for piece in resp.aiter_bytes(
                    64 * 1024
                ):

                    pos += len(piece)

                    yield piece

                await resp.aclose()

                if (
                    last is None
                    or pos > last
                    or resp.status_code == 200
                ):

                    break

                # Request next chunk.
                resp = await open_chunk(
                    src,
                    pos,
                    min(
                        last,
                        pos + _CHUNK - 1,
                    ),
                )

                if resp.status_code not in (200, 206):

                    print(
                        "Next stream chunk failed: "
                        f"HTTP {resp.status_code}"
                    )

                    await resp.aclose()
                    break

        finally:

            await resp.aclose()

    # --------------------------------------------------------
    # RESPONSE HEADERS
    # --------------------------------------------------------

    headers = {
        "accept-ranges": "bytes",
    }

    if last is not None:

        headers["content-length"] = str(
            last - start + 1
        )

    if asked:

        headers["content-range"] = (
            f"bytes {start}-{last}/"
            f"{total if total else '*'}"
        )

    return StreamingResponse(
        body(),
        status_code=206 if asked else 200,
        media_type=src.mime,
        headers=headers,
    )


# ============================================================
# MP3 DOWNLOAD
# ============================================================

@app.get("/api/download/{video_id}")
async def download(
    video_id: str,
):

    """
    Convert to MP3 using FFmpeg.

    Cached on disk.
    """

    _check_id(video_id)

    try:

        path = await youtube.get_mp3(
            video_id
        )

    except DownloadError as e:

        raise HTTPException(
            404,
            f"Song unavailable: {e}",
        ) from e

    if not path.exists():

        raise HTTPException(
            500,
            "Conversion failed (is ffmpeg installed?)",
        )

    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=f"{video_id}.mp3",
    )


# ============================================================
# LYRICS
# ============================================================

@app.get("/api/lyrics/{video_id}")
async def get_lyrics(
    video_id: str,
    request: Request,
    title: str | None = Query(
        None,
        max_length=300,
        description="Override song title (skips YouTube lookup)",
    ),
    artist: str | None = Query(
        None,
        max_length=200,
    ),
    duration: int | None = Query(
        None,
        ge=1,
        description="Seconds; used to pick the right version",
    ),
):

    """
    Lyrics from lrclib.net.

    lines = timed lines for karaoke-style highlighting.
    """

    _check_id(video_id)

    if title:

        meta = {
            "title": title,
            "artist": artist,
            "duration": duration,
        }

    else:

        try:

            meta = dict(
                await youtube.get_meta(
                    video_id
                )
            )

        except DownloadError as e:

            raise HTTPException(
                404,
                f"Song unavailable: {e}",
            ) from e

        if artist:
            meta["artist"] = artist

        if duration:
            meta["duration"] = duration

    cache_key = (
        f"{video_id}|"
        f"{meta.get('title')}|"
        f"{meta.get('artist')}|"
        f"{meta.get('duration')}"
    )

    try:

        found = await lyrics.find(
            request.app.state.http,
            cache_key,
            meta,
        )

    except httpx.HTTPError as e:

        raise HTTPException(
            502,
            f"Lyrics service error: {e}",
        ) from e

    if not found:

        raise HTTPException(
            404,
            "Lyrics not found",
        )

    return {
        "id": video_id,
        **found,
    }
