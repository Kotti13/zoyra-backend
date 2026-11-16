
FROM python:3.12-slim

# Install FFmpeg and certificates
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Deno
COPY --from=denoland/deno:bin-2.6.0 /deno /usr/local/bin/deno

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY app ./app

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
