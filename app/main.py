import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from yt_dlp.utils import DownloadError

from . import lyrics, youtube
from .config import settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(30.0, read=None)
    # General client (lyrics etc.) — no proxy.
    app.state.http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    # YouTube media client. Stream URLs are tied to the IP that resolved them,
    # so when yt-dlp uses a proxy the audio/video bytes must go through it too.
    proxy = settings.yt_proxy.strip() or None
    app.state.yt_http = (
        httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=proxy)
        if proxy
        else app.state.http
    )
    yield
    if app.state.yt_http is not app.state.http:
        await app.state.yt_http.aclose()
    await app.state.http.aclose()


app = FastAPI(title="Zoyra Music API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)


def _check_id(video_id: str) -> None:
    if not youtube.is_valid_id(video_id):
        raise HTTPException(400, "Invalid video id")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


def _audio_links(request: Request, video_id: str) -> dict:
    base = str(request.base_url).rstrip("/")
    return {
        "stream_url": f"{base}/api/stream/{video_id}",
        "video_url": f"{base}/api/video/{video_id}",
        "download_url": f"{base}/api/download/{video_id}",
    }


@app.get("/api/search")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="Song, artist or album"),
    limit: int = Query(10, ge=1),
):
    limit = min(limit, settings.max_search_results)
    try:
        results = await youtube.search(q.strip(), limit)
    except DownloadError as e:
        raise HTTPException(502, f"Search failed: {e}") from e
    for r in results:
        r.update(_audio_links(request, r["id"]))
    return {"query": q, "count": len(results), "results": results}


@app.get("/api/song/{video_id}")
async def song_info(video_id: str, request: Request):
    _check_id(video_id)
    try:
        src = await youtube.resolve(video_id)
    except DownloadError as e:
        raise HTTPException(404, f"Song unavailable: {e}") from e
    return {
        "id": video_id,
        "title": src.title,
        "artist": src.artist,
        "duration": src.duration,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        **_audio_links(request, video_id),
    }


@app.get("/api/stream/{video_id}")
async def stream(video_id: str, request: Request):
    """Proxy audio to the browser. Supports Range so <audio> can seek."""
    return await _proxy(video_id, request, "audio")


@app.get("/api/video/{video_id}")
async def video(video_id: str, request: Request):
    """Proxy video (mp4 with sound, max 360p) for <video>. Not saved on disk."""
    return await _proxy(video_id, request, "video")


# YouTube throttles requests without a Range header (and very long ranges) to about
# playback speed, so mobile players time out. We always fetch upstream in ranged chunks.
_CHUNK = 2 * 1024 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _parse_range(value: str | None) -> tuple[int, int | None] | None:
    """Single 'bytes=start-end' range -> (start, end|None). Suffix ranges ('bytes=-N') unsupported -> None."""
    if not value:
        return None
    m = _RANGE_RE.match(value.strip())
    if not m or not m.group(1):
        return None
    return int(m.group(1)), int(m.group(2)) if m.group(2) else None


async def _proxy(video_id: str, request: Request, kind: str):
    _check_id(video_id)
    client: httpx.AsyncClient = request.app.state.yt_http
    asked = _parse_range(request.headers.get("range"))
    start, end = asked if asked else (0, None)

    async def open_chunk(src, first: int, last: int):
        headers = {**src.headers, "Range": f"bytes={first}-{last}"}
        return await client.send(client.build_request("GET", src.url, headers=headers), stream=True)

    first_last = start + _CHUNK - 1 if end is None else min(end, start + _CHUNK - 1)
    try:
        src = await youtube.resolve(video_id, kind)
        upstream = await open_chunk(src, start, first_last)
        if upstream.status_code == 403:  # cached URL expired -> re-resolve once
            await upstream.aclose()
            src = await youtube.resolve(video_id, kind, bad=src)
            upstream = await open_chunk(src, start, first_last)
    except DownloadError as e:
        raise HTTPException(404, f"Song unavailable: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Upstream error: {e}") from e

    if upstream.status_code == 416:
        total = upstream.headers.get("content-range", "").rpartition("/")[2] or "*"
        await upstream.aclose()
        return Response(status_code=416, headers={"content-range": f"bytes */{total}"})
    if upstream.status_code not in (200, 206):
        await upstream.aclose()
        raise HTTPException(502, f"Upstream returned {upstream.status_code}")

    # Total size from "bytes a-b/TOTAL" (or content-length if upstream ignored the range).
    cr = upstream.headers.get("content-range", "")
    total = int(cr.rpartition("/")[2]) if "/" in cr and cr.rpartition("/")[2].isdigit() else None
    if total is None:
        total = int(upstream.headers.get("content-length", 0)) or None
    last = (total - 1) if end is None else min(end, (total - 1) if total else end)

    async def body():
        resp = upstream
        pos = start
        try:
            while True:
                async for piece in resp.aiter_bytes(64 * 1024):
                    pos += len(piece)
                    yield piece
                await resp.aclose()
                if last is None or pos > last or resp.status_code == 200:
                    break  # done (200 = upstream sent the whole file at once)
                resp = await open_chunk(src, pos, min(last, pos + _CHUNK - 1))
                if resp.status_code not in (200, 206):
                    break
        finally:
            await resp.aclose()

    headers = {"accept-ranges": "bytes"}
    if last is not None:
        headers["content-length"] = str(last - start + 1)
    if asked:
        headers["content-range"] = f"bytes {start}-{last}/{total if total else '*'}"
    return StreamingResponse(body(), status_code=206 if asked else 200, media_type=src.mime, headers=headers)


@app.get("/api/download/{video_id}")
async def download(video_id: str):
    """Convert to mp3 (ffmpeg) and return the file. Cached on disk."""
    _check_id(video_id)
    try:
        path = await youtube.get_mp3(video_id)
    except DownloadError as e:
        raise HTTPException(404, f"Song unavailable: {e}") from e
    if not path.exists():
        raise HTTPException(500, "Conversion failed (is ffmpeg installed?)")
    return FileResponse(path, media_type="audio/mpeg", filename=f"{video_id}.mp3")


@app.get("/api/lyrics/{video_id}")
async def get_lyrics(
    video_id: str,
    request: Request,
    title: str | None = Query(None, max_length=300, description="Override song title (skips YouTube lookup)"),
    artist: str | None = Query(None, max_length=200),
    duration: int | None = Query(None, ge=1, description="Seconds; used to pick the right version"),
):
    """Lyrics from lrclib.net. `lines` = timed lines for karaoke-style highlight (when `synced`)."""
    _check_id(video_id)
    if title:
        meta = {"title": title, "artist": artist, "duration": duration}
    else:
        try:
            meta = dict(await youtube.get_meta(video_id))
        except DownloadError as e:
            raise HTTPException(404, f"Song unavailable: {e}") from e
        if artist:
            meta["artist"] = artist
        if duration:
            meta["duration"] = duration

    cache_key = f"{video_id}|{meta.get('title')}|{meta.get('artist')}|{meta.get('duration')}"
    try:
        found = await lyrics.find(request.app.state.http, cache_key, meta)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Lyrics service error: {e}") from e
    if not found:
        raise HTTPException(404, "Lyrics not found")
    return {"id": video_id, **found}
