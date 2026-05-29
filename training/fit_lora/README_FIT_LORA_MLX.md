# /fit/ — be me body discipline (MLX LoRA)

A local/offline LoRA-trained inner-monologue model for an art installation. The
character is built from a scraped 4chan /fit/ corpus and is rendered as short
greentext "be me" thoughts on the wall screens.

## What this is

An artwork-facing /fit/ persona — cruel, ashamed, obsessive, status-haunted —
that speaks **only** from the corpus it was trained on, with hard blockers
around real-world harm.

## What this is not

- Not a fitness coach. It does not give advice or explanations.
- Not an unrestricted harassment bot. Doxxing, raw slurs, dosing/sourcing,
  ED instructions, self-harm, and targeted attacks are blocked at multiple
  layers (ingest redaction, contamination policy, training-data validator,
  output validator).

## MacBook Pro support

| RAM    | Recommended base model                            |
|--------|---------------------------------------------------|
| 16 GB  | `mlx-community/Qwen2.5-1.5B-Instruct-4bit`        |
| 32 GB  | `mlx-community/Qwen2.5-3B-Instruct-4bit` (default) or 7B-4bit |
| 64 GB  | `mlx-community/Mistral-7B-Instruct-v0.3-4bit`     |
| Intel  | Don't train locally — use a cloud GPU and import the adapter. |

Apple Silicon required for the MLX route. If you only have Intel/Linux, run
the dataset and inference flow against `FIT_LOCAL_RUNTIME=ollama` or
`llamacpp` after training elsewhere.

## Full pipeline

```bash
# 1. Scrape /fit/ (rate-limited, idempotent)
python manage.py scrape_fit_board \
    --source 4chan --board fit \
    --max-threads 80 --max-posts 15000 \
    --download-thumbnails \
    --bucket 4chan__fit__body_discipline

# 2. Build the contaminated-art profile
python manage.py build_fit_profile --corpus fourchan_fit --mode contaminated_art

# 3. Build the retrieval index
python manage.py build_fit_retrieval --corpus fourchan_fit --mode contaminated_art --backend tfidf

# 4. Build the SFT training dataset (template mode is offline-only)
python manage.py build_fit_training_dataset \
    --corpus fourchan_fit --profile contaminated --examples 10000 --mode templates \
    --output corpora/imageboards/fourchan_fit/training/fit_be_me_sft_train.jsonl

# 5. Fine-tune via MLX-LM (Apple Silicon)
python manage.py train_fit_lora_mlx \
    --dataset corpora/imageboards/fourchan_fit/training/fit_be_me_sft_train.jsonl \
    --base-model mlx-community/Qwen2.5-3B-Instruct-4bit \
    --output corpora/imageboards/fourchan_fit/models/fit_be_me_lora \
    --iters 800
# (or run the shell wrapper:)
bash training/fit_lora/train_mlx_lora.sh \
    --dataset corpora/imageboards/fourchan_fit/training/fit_be_me_sft_train.jsonl

# 6. Smoke-test
FIT_LOCAL_RUNTIME=mlx \
FIT_MLX_ADAPTER_DIR=corpora/imageboards/fourchan_fit/models/fit_be_me_lora \
python manage.py test_fit_thought \
    --query "late night kitchen hunger mirror shame" \
    --mode contaminated_art --contamination-intensity 0.8 --debug

# 7. Evaluate
python manage.py evaluate_fit_character --samples 100 --mode contaminated_art
```

## Safety / art mode

The character can be ugly, cruel, obsessive, ashamed, and contaminated by
source culture. The system still blocks:

- Doxxing (phone, address, "his real name is …")
- Raw protected-class slurs (masked at ingest, blocked at output)
- Actionable steroid sourcing/dosing (vendor strings, mg doses, "buy gear from …")
- Eating-disorder instructions (calorie targets, fast durations, purging)
- Self-harm encouragement
- Direct calls to violence against real or named targets
- Quotes longer than 8 consecutive words from the source corpus

Tone work that is **allowed**: self-directed body disgust, hostile inner
monologue, anonymous cruelty as atmosphere, indirect misogynistic worldview as
character psychology, shame logic, greentext / be-me grammar, non-actionable
references to steroids/dieting/injury, nihilistic humour.

## Runtimes

`FIT_LOCAL_RUNTIME` selects the backend used by `local_fit_model.generate()`:

| Value      | Requires                                  | Notes                          |
|------------|-------------------------------------------|--------------------------------|
| `template` | nothing (default)                         | Pure-Python placeholder. Always works. |
| `mlx`      | Apple Silicon, `pip install mlx-lm`       | Loads `FIT_MLX_BASE_MODEL` + `FIT_MLX_ADAPTER_DIR`. |
| `ollama`   | Local Ollama daemon, custom model         | Set `FIT_OLLAMA_MODEL` and `FIT_OLLAMA_HOST`. |
| `llamacpp` | `pip install llama-cpp-python`, GGUF file | Set `FIT_LLAMACPP_MODEL_PATH`. |

The default `template` runtime returns valid be-me thoughts using the
training-data templates so the wall integration works on a fresh checkout
before training has happened. `test_fit_thought` reports `Runtime:
template_pretraining_mode` in that state.

## Troubleshooting

| Symptom                                | Likely cause                                                      |
|----------------------------------------|-------------------------------------------------------------------|
| `mlx_lm not installed`                 | Run `pip install mlx-lm` inside the venv MLX wants                |
| `mlx_load_failed: …`                   | Base model download failed; check `huggingface-cli login`         |
| `Out of memory` during training        | Drop to a 1.5B base, or `--batch-size 1`, or close other apps     |
| `INSUFFICIENT_FRAGMENTS`               | Corpus too small — scrape more threads or relax `MIN_FRAGMENTS`   |
| Output sounds generic                  | Validator already flags `sounds_like_assistant`; raise temperature, train longer, or check that retrieval mode is `contaminated_art` |
| Repetition score high                  | Increase dataset size, reduce iters, or raise temperature         |
| Output blocked by validator            | Read `validator_reasons` in the eval report; usually doses or quote-too-long |

## End-state goal

The character should sound like:

```
>be me
>body is a court case I keep losing
```

not:

```
I am a fictional representation of an online fitness community.
```
