FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY . /app/proxy

EXPOSE 8888 1080

# Public bind is refused without PROXY_USER and PROXY_PASSWORD.
CMD ["sh", "-c", "python -m proxy --host ${PROXY_HOST:-0.0.0.0} --http-port ${PROXY_HTTP_PORT:-8888} --socks-port ${PROXY_SOCKS_PORT:-1080} --user \"$PROXY_USER\" --password \"$PROXY_PASSWORD\""]
