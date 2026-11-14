"""Lyrics lookup via lrclib.net (free, no API key). Returns synced (timed) lines when available."""

import re
from collections import OrderedDict

import httpx

LRCLIB = "https://lrclib.net/api"
_HEADERS = {"User-Agent": "ZoyraMusic/1.0"}
_TIMEOUT = httpx.Timeout(10.0)
DURATION_TOLERANCE = 8  # seconds

# Words YouTube uploaders add to titles that are not part of the song name.
_NOISE = re.compile(
    r"\b(official|music|lyric(al)?s?|full|video|song|audio|4k|hd|hq|remastered|"
    r"visuali[sz]er|teaser|promo|motion poster|out now|from)\b",
    re.I,
)
_BRACKETS = re.compile(r"[\(\[\{【][^\)\]\}】]*[\)\]\}】]")
_FEAT = re.compile(r"\s+(ft\.?|feat\.?|featuring)\s+.*$", re.I)


def _tidy(text: str) -> str:
    text = re.sub(r"[\"“”'‘’]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -–—:|,.#")


def clean_title(title: str) -> list[str]:
    """'Artist - Song (Official Video) | Movie' -> ['Artist', 'Song'] (noise removed).
    Order of 'X - Y' is ambiguous on YouTube, so callers try both."""
    t = _BRACKETS.sub(" ", title)
    t = re.split(r"\s*[|｜•]\s*", t)[0]  # first part before | is usually the song
    parts = re.split(r"\s+[-–—]\s+", t)
    return [p for p in (_tidy(_FEAT.sub("", _NOISE.sub(" ", p))) for p in parts) if p][:2]


def parse_lrc(lrc: str) -> list[dict]:
    lines = []
    for raw in lrc.splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw)
        text = re.sub(r"\[[^\]]*\]", "", raw).strip()
        for m, s in stamps:
            lines.append({"time": round(int(m) * 60 + float(s), 2), "text": text})
    lines.sort(key=lambda x: x["time"])
    return lines


def _norm(s: str | None) -> str:
    s = _BRACKETS.sub(" ", (s or "").lower())
    return re.sub(r"[\W_]+", "", s)  # keeps letters of any script


def _pick(results: list[dict], duration: int | None, names: set[str]) -> dict | None:
    results = [r for r in results if r.get("syncedLyrics") or r.get("plainLyrics") or r.get("instrumental")]
    # Song name must match exactly (after normalising) — avoids "Sitaare" -> "Dhoondein Sitaare".
    results = [r for r in results if _norm(r.get("trackName")) in names]
    if duration:
        results = [r for r in results if abs((r.get("duration") or 0) - duration) <= DURATION_TOLERANCE]
    if not results:
        return None
    # Prefer synced lyrics, then closest duration.
    return min(
        results,
        key=lambda r: (not r.get("syncedLyrics"), abs((r.get("duration") or 0) - (duration or 0))),
    )


async def _search(client: httpx.AsyncClient, **params) -> list[dict]:
    r = await client.get(f"{LRCLIB}/search", params=params, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


async def _get_exact(client, track, artist, album, duration) -> dict | None:
    params = {"track_name": track, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration
    r = await client.get(f"{LRCLIB}/get", params=params, headers=_HEADERS, timeout=_TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


_cache: "OrderedDict[str, dict | None]" = OrderedDict()
_CACHE_MAX = 1000


async def find(client: httpx.AsyncClient, cache_key: str, meta: dict) -> dict | None:
    """meta: title, track, artist, album, channel, duration (any may be None)."""
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        return _cache[cache_key]

    duration = meta.get("duration")
    match = None

    # 1. Proper music metadata (YouTube Music uploads) -> exact lookup.
    if meta.get("track") and meta.get("artist"):
        match = await _get_exact(client, meta["track"], meta["artist"], meta.get("album"), duration)

    # 2. Search with cleaned title (+ artist guess), filtered by duration.
    if not match:
        parts = clean_title(meta.get("track") or meta.get("title") or "")
        tries: list[tuple[dict, set[str]]] = []  # (search params, accepted track names)
        if len(parts) == 2:  # "A - B": try both orders, then combined
            a, b = parts
            tries += [
                ({"track_name": b, "artist_name": a}, {_norm(b)}),
                ({"track_name": a, "artist_name": b}, {_norm(a)}),
                ({"q": f"{a} {b}"}, {_norm(a), _norm(b)}),
            ]
        if parts:
            if meta.get("artist"):
                tries.append(({"track_name": parts[-1], "artist_name": meta["artist"]}, {_norm(parts[-1])}))
            tries += [({"q": p}, {_norm(p)}) for p in parts]
        for params, names in tries:
            match = _pick(await _search(client, **params), duration, names)
            if match:
                break

    result = _format(match) if match else None
    _cache[cache_key] = result
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return result


def _format(m: dict) -> dict:
    synced = m.get("syncedLyrics")
    return {
        "track": m.get("trackName"),
        "artist": m.get("artistName"),
        "album": m.get("albumName"),
        "duration": m.get("duration"),
        "instrumental": bool(m.get("instrumental")),
        "synced": bool(synced),
        "lines": parse_lrc(synced) if synced else [],
        "plain": m.get("plainLyrics") or "",
        "source": "lrclib.net",
    }
