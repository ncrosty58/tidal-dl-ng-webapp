#!/bin/sh
set -e

# tidal-dl-ng doesn't auto-detect ffmpeg on its own (only the desktop GUI's
# "browse" button does); without this, FLAC extraction and video conversion
# silently degrade even though ffmpeg is bundled in this image. Safe to run
# on every start: it's idempotent and creates settings.json on first run if
# it doesn't exist yet.
FFMPEG_PATH="$(command -v ffmpeg || true)"
if [ -n "$FFMPEG_PATH" ]; then
    tidal-dl-ng cfg path_binary_ffmpeg "$FFMPEG_PATH" >/dev/null 2>&1 || true
fi

exec "$@"
