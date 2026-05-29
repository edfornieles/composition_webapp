#!/usr/bin/env bash
# Train a /fit/ be-me LoRA adapter using MLX-LM on Apple Silicon.
#
# Usage:
#   bash training/fit_lora/train_mlx_lora.sh \
#     --dataset corpora/imageboards/fourchan_fit/training/fit_be_me_sft_train.jsonl \
#     --base-model mlx-community/Qwen2.5-3B-Instruct-4bit \
#     --output corpora/imageboards/fourchan_fit/models/fit_be_me_lora \
#     --iters 800
#
# Requires: macOS arm64 (Apple Silicon), Python 3.10+, and ~12-16 GB free RAM
# for a 3B 4-bit base model.

set -euo pipefail

DATASET=""
BASE_MODEL="mlx-community/Qwen2.5-3B-Instruct-4bit"
OUTPUT="corpora/imageboards/fourchan_fit/models/fit_be_me_lora"
ITERS=800
BATCH=1
LR="2e-5"
RANK=16

while [ $# -gt 0 ]; do
  case "$1" in
    --dataset) DATASET="$2"; shift 2 ;;
    --base-model) BASE_MODEL="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --iters) ITERS="$2"; shift 2 ;;
    --batch-size) BATCH="$2"; shift 2 ;;
    --learning-rate) LR="$2"; shift 2 ;;
    --lora-rank) RANK="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$DATASET" ]; then
  echo "ERROR: --dataset is required" >&2
  exit 2
fi

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "ERROR: MLX requires Apple Silicon (Darwin/arm64). Use a cloud GPU instead." >&2
  exit 3
fi

if [ ! -f "$DATASET" ]; then
  echo "ERROR: dataset not found: $DATASET" >&2
  exit 2
fi

# Stage train/valid jsonl as required by mlx_lm.lora
DATA_DIR="$(dirname "$DATASET")"
TRAIN="$DATA_DIR/train.jsonl"
VALID="$DATA_DIR/valid.jsonl"
if [ ! -f "$TRAIN" ] || [ ! -f "$VALID" ]; then
  python3 - "$DATASET" "$TRAIN" "$VALID" <<'PY'
import sys, math
src, train_p, valid_p = sys.argv[1:]
with open(src, encoding="utf-8") as f:
    lines = [l for l in f.read().splitlines() if l.strip()]
cut = max(1, math.floor(len(lines) * 0.95))
open(train_p, "w", encoding="utf-8").write("\n".join(lines[:cut]) + "\n")
open(valid_p, "w", encoding="utf-8").write("\n".join(lines[cut:]) + "\n")
print(f"Staged {cut} train / {len(lines)-cut} valid")
PY
fi

# Set up venv if missing
VENV_DIR="${VENV_DIR:-.venv-fitlora}"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip >/dev/null
python -m pip install -q mlx-lm

mkdir -p "$OUTPUT"

python -m mlx_lm.lora \
  --train \
  --model "$BASE_MODEL" \
  --data "$DATA_DIR" \
  --iters "$ITERS" \
  --batch-size "$BATCH" \
  --learning-rate "$LR" \
  --lora-layers "$RANK" \
  --adapter-path "$OUTPUT"

# Write a small report
python3 - "$OUTPUT" "$BASE_MODEL" "$ITERS" "$BATCH" "$LR" "$RANK" "$DATASET" <<'PY'
import json, sys, time
out, base, iters, batch, lr, rank, ds = sys.argv[1:]
import os
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "training_report.json"), "w", encoding="utf-8") as f:
    json.dump({
        "status": "ok",
        "base_model": base, "iters": int(iters), "batch_size": int(batch),
        "learning_rate": float(lr), "lora_rank": int(rank), "dataset": ds,
        "finished_at": int(time.time()),
    }, f, indent=2)
print(f"Wrote {out}/training_report.json")
PY

echo "Training complete. Adapter: $OUTPUT"
