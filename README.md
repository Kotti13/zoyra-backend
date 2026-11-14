# Zoyra Music API

FastAPI backend: search songs on YouTube, stream audio to the frontend, convert to mp3.

## Requirements
- Python 3.12 (`.python-version`), [uv](https://docs.astral.sh/uv/)
- `ffmpeg` on PATH (only needed for `/api/download`)
- A JS runtime on PATH: `deno`, `node` or `bun` (YouTube needs it to unlock formats; solver script comes from `yt-dlp-ejs`)

## Run
```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env          # set CORS_ORIGINS to your frontend URL
.venv/bin/uvicorn app.main:app --reload --port 8000
```
Docs: http://localhost:8000/docs

## Endpoints
| Method | Path | Returns |
|---|---|---|
| GET | `/api/search?q=<song/artist>&limit=10` | `{query, count, results: [{id, title, artist, duration, thumbnail, views, stream_url, video_url, download_url}]}` |
| GET | `/api/song/{id}` | song metadata + `stream_url`, `download_url` |
| GET | `/api/stream/{id}` | audio (m4a/AAC), supports `Range` → seeking works |
| GET | `/api/video/{id}` | video mp4 with sound (max 360p), supports `Range`, not saved |
| GET | `/api/lyrics/{id}` | lyrics from lrclib.net: `{synced, lines: [{time, text}], plain, track, artist}`; 404 if not found. Optional `?title=&artist=&duration=` skips YouTube lookup (faster) |
| GET | `/api/download/{id}` | converted mp3 file (cached in `downloads/`) |
| GET | `/api/health` | `{status: "ok"}` |

## Frontend usage
```js
const API = "http://localhost:8000";

const { results } = await fetch(`${API}/api/search?q=${encodeURIComponent(q)}`).then(r => r.json());

// play: browser handles buffering + seeking via Range requests
audio.src = `${API}/api/stream/${results[0].id}`;
audio.play();

// download mp3
window.location.href = `${API}/api/download/${results[0].id}`;
```

## Notes
- First play of a song takes ~5-7s: YouTube links only become valid a few seconds after lookup.
- React Native: CORS not needed. Use your machine's LAN IP (or `10.0.2.2` on Android emulator) instead of `localhost`.
- Postman: import `zoyra.postman_collection.json`.
- Stream URLs from YouTube are cached in memory (`STREAM_URL_TTL`) and re-resolved automatically on expiry (403).
- Keep yt-dlp updated (`uv pip install -U yt-dlp`) — YouTube changes break old versions.

## Deploy on Render
1. New → Web Service → connect this repo. Runtime: **Docker** (uses `Dockerfile`, which installs ffmpeg + deno).
2. No env vars required. Optional:
   - `CORS_ORIGINS` – only for browser clients.
   - `YT_PROXY` – residential proxy (`http://user:pass@host:port` or `socks5://...`) if YouTube blocks the server IP (*"Failed to extract any player response"* / *"not a bot"*). All YouTube lookups and audio/video bytes go through it, so it uses proxy bandwidth (~4 MB per song).
   - `YT_COOKIES` – if logs show *"Sign in to confirm you're not a bot"*. Export cookies.txt from a logged-in YouTube session (use a throwaway account) and paste its contents. Keep it secret.
3. Check: `https://<service>.onrender.com/api/health` → `{"status":"ok"}`.

Free plan sleeps after 15 min idle (first request ~30-50s) and `downloads/` is wiped on restart.
