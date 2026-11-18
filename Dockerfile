FROM python:3.12-slim

# Install FFmpeg and certificates
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Deno
COPY --from=denoland/deno:bin-2.6.0 /deno /usr/local/bin/deno

# Hugging Face Spaces runs containers as UID 1000; use the same user everywhere.
RUN useradd -m -u 1000 user
WORKDIR /app
RUN chown user:user /app

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY --chown=user:user app ./app

USER user

# Render sets $PORT; Hugging Face Spaces expects 7860 (app_port in README.md).
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
