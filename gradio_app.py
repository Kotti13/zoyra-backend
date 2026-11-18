"""Hugging Face Spaces (Gradio SDK) entrypoint.

Serves the normal FastAPI app (all /api/* routes) and mounts a small Gradio
status page at "/". Spaces runs this file with `python gradio_app.py`.
"""

import io
import os
import platform
import shutil
import stat
import urllib.request
import zipfile
from pathlib import Path

DENO_VERSION = "2.6.0"
DENO_DIR = Path(__file__).resolve().parent / ".bin"


def ensure_deno() -> None:
    """yt-dlp needs a JS runtime. Spaces' Gradio image has none, so fetch deno once.

    Must run before `app.main` is imported: app/youtube.py looks up the runtime at import.
    """
    if shutil.which("deno") or shutil.which("node") or shutil.which("bun"):
        return
    arch = "aarch64" if platform.machine() in ("aarch64", "arm64") else "x86_64"
    url = (
        f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/"
        f"deno-{arch}-unknown-linux-gnu.zip"
    )
    DENO_DIR.mkdir(exist_ok=True)
    target = DENO_DIR / "deno"
    if not target.exists():
        print(f"Downloading deno {DENO_VERSION} ...")
        with urllib.request.urlopen(url, timeout=120) as r:
            zipfile.ZipFile(io.BytesIO(r.read())).extract("deno", DENO_DIR)
        target.chmod(target.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{DENO_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


ensure_deno()

import gradio as gr  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as api  # noqa: E402

with gr.Blocks(title="Zoyra Music API") as demo:
    gr.Markdown(
        "# Zoyra Music API\n"
        "Backend is running. Use the REST endpoints:\n\n"
        "- `GET /api/health`\n"
        "- `GET /api/search?q=...`\n"
        "- `GET /api/stream/{video_id}`\n"
        "- `GET /docs` (interactive API docs)"
    )

# API routes are registered first, so they win over the Gradio mount at "/".
app = gr.mount_gradio_app(api, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
