#!/usr/bin/env bash
# Quick smoke test: generate one sample using the trained MLX-LM LoRA.
#
# Usage:
#   bash training/fit_lora/generate_from_mlx.sh \
#     --base-model mlx-community/Qwen2.5-3B-Instruct-4bit \
#     --adapter corpora/imageboards/fourchan_fit/models/fit_be_me_lora

set -euo pipefail

BASE_MODEL="mlx-community/Qwen2.5-3B-Instruct-4bit"
ADAPTER="corpora/imageboards/fourchan_fit/models/fit_be_me_lora"
PROMPT_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --base-model) BASE_MODEL="$2"; shift 2 ;;
    --adapter) ADAPTER="$2"; shift 2 ;;
    --prompt-file) PROMPT_FILE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

VENV_DIR="${VENV_DIR:-.venv-fitlora}"
# shellcheck disable=SC1090
[ -d "$VENV_DIR" ] && source "$VENV_DIR/bin/activate" || true

if [ -n "$PROMPT_FILE" ] && [ -f "$PROMPT_FILE" ]; then
  PROMPT="$(cat "$PROMPT_FILE")"
else
  PROMPT='State:
time: late night
place: kitchen
body_focus: stomach
object: fridge light
dominant_drive: discipline
dominant_fear: softness
contamination: intensity=0.8
Write one short /fit/ thought in be-me/greentext logic.'
fi

python -m mlx_lm.generate \
  --model "$BASE_MODEL" \
  --adapter-path "$ADAPTER" \
  --max-tokens 96 \
  --temp 0.95 \
  --prompt "$PROMPT"
