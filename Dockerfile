FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/ncrosty58/tidal-dl"
LABEL org.opencontainers.image.description="Self-contained web UI for tidal-dl-ng: paste a TIDAL URL, watch the download stream live"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# ffmpeg is required by tidal-dl-ng for FLAC extraction / MP4 video conversion
RUN apk add --no-cache ffmpeg

COPY requirements.txt .
COPY vendor/ vendor/
RUN apk add --no-cache --virtual .build-deps gcc musl-dev libffi-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apk del .build-deps

COPY . .

# Run as a non-root user; uid 1000 matches the typical host owner of the
# bind-mounted downloads volume and the tidal_dl_ng config volume.
RUN addgroup -g 1000 -S app && adduser -u 1000 -S app -G app -h /home/app && \
    mkdir -p /home/app/.config/tidal_dl_ng /downloads && \
    chown -R app:app /app /home/app /downloads

ENV HOME=/home/app \
    TIDAL_DL_BIN=tidal-dl-ng \
    DOWNLOAD_TIMEOUT=0 \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=5050

USER app

EXPOSE 5050

# manifest.json is served without touching tidal-dl-ng, so it's a cheap liveness probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD wget -qO /dev/null http://127.0.0.1:5050/static/manifest.json || exit 1

# Single worker (the in-process output_queue/current_process state assumes one
# process); gthread keeps the SSE stream and concurrent requests responsive.
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "1", "--threads", "8", \
     "--timeout", "300", "--access-logfile", "-", "app:app"]
