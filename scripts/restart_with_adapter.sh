#!/usr/bin/env bash
# Restart the dev server with a specific adapter path.
# Usage: bash scripts/restart_with_adapter.sh training/fit_lora/adapters/fit_anon_v2_greentext

set -e
ADAPTER="${1:-training/fit_lora/adapters/fit_anon_v1}"
ROOT="/Volumes/Oom/aggregation/composition_webapp-main"
LOG="/tmp/fitboard_server.log"

cd "$ROOT"
pkill -f "manage.py runserver 8765" 2>/dev/null || true
sleep 2

DB_ENGINE=sqlite \
IMAGEBOARD_LM_BACKEND=mlx \
FIT_MLX_BASE_MODEL=mlx-community/Qwen2.5-3B-Instruct-4bit \
IMAGEBOARD_MLX_MODEL=mlx-community/Qwen2.5-3B-Instruct-4bit \
IMAGEBOARD_MLX_ADAPTER="$ADAPTER" \
nohup .venv/bin/python manage.py runserver 8765 > "$LOG" 2>&1 &

echo "started server with adapter: $ADAPTER (pid $!)"
echo "log: $LOG"
