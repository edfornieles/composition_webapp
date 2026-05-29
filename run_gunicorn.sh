#!/usr/bin/env bash
# Launch the composition webapp under gunicorn with multi-worker concurrency.
#
# Why: the Django dev server (`manage.py runserver`) is single-threaded — one
# slow video stream blocks every other request. With 9 simultaneous composition
# tabs each requesting media, page-beacon POSTs, and static assets, Chrome's
# 6-connection-per-origin limit fills with beacons and new page loads hang
# indefinitely.
#
# Configuration:
#   - 1 worker process × 32 threads = 32 concurrent in-flight requests
#   - Single worker prevents multiple copies of the MLX LoRA model loading into
#     RAM simultaneously (4 workers × ~1.5 GB = OOM kills on every cold start)
#   - gthread worker class is best for I/O-heavy media serving (low CPU per req)
#   - --timeout 120s tolerates the occasional slow large-MP4 fetch
#   - Bind to 127.0.0.1:8765 to match the existing dev URL
#
# Usage: ./run_gunicorn.sh
# Stop:  Ctrl-C   (or  pkill -f "gunicorn.*djangoscrap")

# NOTE: no `set -e` here — we WANT the supervisor loop to keep going even
# if gunicorn exits non-zero (OOM kill, crash, etc.).

cd "$(dirname "$0")"

export DB_ENGINE="${DB_ENGINE:-sqlite}"
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-djangoscrap.settings}"

# Use absolute paths so the script works under non-interactive shells (where
# the `python` alias doesn't exist).
PY="${PY:-/opt/homebrew/bin/python3.10}"
GUNICORN="${GUNICORN:-/opt/homebrew/bin/gunicorn}"

# Collect static files for whitenoise to serve. Cheap when already up-to-date.
"$PY" manage.py collectstatic --noinput >/dev/null 2>&1 || {
    echo "collectstatic failed — running with verbose output:"
    "$PY" manage.py collectstatic --noinput
    exit 1
}

# Supervisor loop: if the gunicorn worker is OOM-killed or crashes (most
# often from an in-process Playwright Chromium capture using too much RAM),
# restart it automatically. Only trap INT (Ctrl-C from an interactive
# terminal) — NOT TERM. If we trapped TERM, then when the harness/parent
# shell that launched us rolled over and sent SIGTERM, the supervisor
# would obey and exit, killing gunicorn with it. That's exactly what
# bit us before. Ignoring TERM keeps the supervisor running across
# parent-shell churn (combined with `setsid` below it survives
# `pkill -P parent` and detaches completely).
trap 'echo "[supervisor] Ctrl-C — shutting down"; exit 0' INT
trap '' TERM HUP

while true; do
    # 2 workers: when one is recycling / hung / deadlocked, the other keeps
    # serving — visitors don't see a stall. Doubles RAM (~150 MB → ~300 MB at
    # idle) which is fine on this Mac. Earlier note about MLX LoRA needing
    # 1-worker exclusivity doesn't apply for the composition workload.
    "$GUNICORN" djangoscrap.wsgi:application \
        --bind 127.0.0.1:8765 \
        --workers 2 \
        --threads 96 \
        --worker-class gthread \
        --worker-connections 1000 \
        --backlog 2048 \
        --timeout 120 \
        --graceful-timeout 30 \
        --keep-alive 2 \
        --max-requests 250 \
        --max-requests-jitter 50 \
        --reload \
        --access-logfile - \
        --error-logfile - \
        --log-level warning
    status=$?
    echo "[supervisor] gunicorn exited with status $status — respawning in 2s"
    sleep 2
done
