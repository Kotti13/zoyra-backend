"""YouTube Music catalogue (search, up next, artist, album, suggestions) via ytmusicapi.

Metadata only: playback still goes through youtube.resolve() / /api/stream/{id}. ytmusicapi is
synchronous (requests), so every call runs in a worker thread. Results use the same shape as
/api/search (id, title, artist, duration, thumbnail) so the app can treat both the same way.
Unofficial API: it can break when YouTube Music changes, so failures surface as HTTP 502.
"""

import asyncio
import re
import time

from ytmusicapi import YTMusic

_yt: YTMusic | None = None
_CACHE_TTL = 300
_cache: dict[str, tuple[float, object]] = {}
_CACHE_MAX = 500
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class YTMusicError(Exception):
    """Upstream failure (network, parsing, YouTube Music changed)."""


def is_valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value))


def _client() -> YTMusic:
    global _yt
    if _yt is None:
        _yt = YTMusic()
    return _yt


def _thumb(thumbs: list[dict] | None, video_id: str | None = None) -> str:
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        return best.get("url", "")
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def _seconds(text: str | None) -> int | None:
    if not text:
        return None
    try:
        secs = 0
        for part in text.split(":"):
            secs = secs * 60 + int(part)
        return secs
    except ValueError:
        return None


def _song(row: dict) -> dict | None:
    vid = row.get("videoId")
    if not vid:
        return None
    artists = row.get("artists") or ([{"name": row["artist"]}] if row.get("artist") else [])
    album = row.get("album")
    if not isinstance(album, dict):  # album tracks carry the album name as a plain string
        album = {"name": album} if album else {}
    return {
        "id": vid,
        "title": row.get("title"),
        "artist": ", ".join(a["name"] for a in artists if a.get("name")) or None,
        "artist_id": next((a.get("id") for a in artists if a.get("id")), None),
        "album": album.get("name"),
        "album_id": album.get("id"),
        "duration": row.get("duration_seconds") or _seconds(row.get("length") or row.get("duration")),
        "thumbnail": _thumb(row.get("thumbnails") or row.get("thumbnail"), vid),
        "explicit": bool(row.get("isExplicit")),
    }


def _cached(key: str, fn):
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        value = fn()
    except Exception as e:  # ytmusicapi raises assorted exceptions (network, parsing, YTMusicServerError)
        raise YTMusicError(str(e) or e.__class__.__name__) from e
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (now, value)
    return value


def _search_sync(query: str, kind: str, limit: int) -> list[dict]:
    rows = _client().search(query, filter=kind, limit=limit)
    if kind == "songs":
        return [s for s in (_song(r) for r in rows) if s][:limit]
    out = []
    for r in rows[:limit]:
        out.append(
            {
                "id": r.get("browseId"),
                "title": r.get("title") or r.get("artist"),
                "artist": ", ".join(a["name"] for a in (r.get("artists") or []) if a.get("name")) or r.get("artist"),
                "year": r.get("year"),
                "type": r.get("type") or r.get("resultType"),
                "thumbnail": _thumb(r.get("thumbnails")),
            }
        )
    return [o for o in out if o["id"]]


async def search(query: str, kind: str, limit: int) -> list[dict]:
    return await asyncio.to_thread(_cached, f"s|{kind}|{limit}|{query.lower()}", lambda: _search_sync(query, kind, limit))


async def suggestions(query: str) -> list[str]:
    return await asyncio.to_thread(_cached, f"g|{query.lower()}", lambda: _client().get_search_suggestions(query)[:10])


def _up_next_sync(video_id: str, limit: int) -> list[dict]:
    data = _client().get_watch_playlist(video_id, limit=limit)
    return [s for s in (_song(t) for t in data.get("tracks", [])) if s][:limit]


async def up_next(video_id: str, limit: int) -> list[dict]:
    return await asyncio.to_thread(_cached, f"w|{video_id}|{limit}", lambda: _up_next_sync(video_id, limit))


def _artist_sync(channel_id: str) -> dict:
    a = _client().get_artist(channel_id)
    songs = [s for s in (_song(r) for r in (a.get("songs") or {}).get("results", [])) if s]
    albums = [
        {"id": r.get("browseId"), "title": r.get("title"), "year": r.get("year"), "thumbnail": _thumb(r.get("thumbnails"))}
        for r in (a.get("albums") or {}).get("results", [])
        if r.get("browseId")
    ]
    return {
        "id": channel_id,
        "name": a.get("name"),
        "description": a.get("description"),
        "subscribers": a.get("subscribers"),
        "thumbnail": _thumb(a.get("thumbnails")),
        "songs": songs,
        "albums": albums,
    }


async def artist(channel_id: str) -> dict:
    return await asyncio.to_thread(_cached, f"a|{channel_id}", lambda: _artist_sync(channel_id))


def _album_sync(browse_id: str) -> dict:
    a = _client().get_album(browse_id)
    thumb = _thumb(a.get("thumbnails"))
    tracks = []
    for t in a.get("tracks", []):
        s = _song({**t, "thumbnails": t.get("thumbnails") or a.get("thumbnails")})
        if s:
            s["album"] = a.get("title")
            tracks.append(s)
    return {
        "id": browse_id,
        "title": a.get("title"),
        "artist": ", ".join(x["name"] for x in (a.get("artists") or []) if x.get("name")) or None,
        "year": a.get("year"),
        "thumbnail": thumb,
        "tracks": tracks,
    }


async def album(browse_id: str) -> dict:
    return await asyncio.to_thread(_cached, f"b|{browse_id}", lambda: _album_sync(browse_id))
