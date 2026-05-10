# Pass 2 — deferred work

Pass 1 (runtime metadata, sampling profiles, chat continuity, retrieval merge,
validator routing, smoke tests) is complete. The items below are explicitly
out of Pass 1 scope and should be picked up next.

## §3 — MLX LoRA wiring (`training/fit_lora/*`)

**State today:** `train_mlx_lora.sh` and `generate_from_mlx.sh` exist but are
under-validated. Runtime *can* load an adapter via `IMAGEBOARD_MLX_ADAPTER`,
and the existing `_mlx_generate` already passes `adapter_path` to `mlx_lm.load`
when the directory exists.

**To do:**
- `train_mlx_lora.sh`
  - Validate `train.jsonl` / `valid.jsonl` exist before invoking trainer
  - Print model path, data path, output adapter path before starting
  - Fail loudly if `mlx-lm` (`pip install mlx-lm`) is not on PATH
  - Refuse to overwrite an existing adapter dir without `--force`
  - Document the exact command in the script header
- `generate_from_mlx.sh`
  - Accept prompt arg or stdin
  - Read `IMAGEBOARD_MLX_MODEL` / `IMAGEBOARD_MLX_ADAPTER`
  - Use the same sampling defaults as `runtime_meta.THOUGHT_PROFILE`
  - Print runtime metadata (backend / model / adapter / temperature)
- New env vars to honor consistently across both scripts and `local_fit_model._mlx_paths()`:
  - `IMAGEBOARD_MLX_MODEL` (already supported with `FIT_MLX_BASE_MODEL` fallback)
  - `IMAGEBOARD_MLX_ADAPTER` (already supported with `FIT_MLX_ADAPTER_DIR` fallback)

## §4 — Training dataset quality (`training_dataset.py`)

**This is the largest single risk in the project.** The current dataset
generator is templates-only. Training a LoRA on it will produce a
high-fidelity template generator, not a real voice.

**Required additions:**
- Target-strategy selector: `real_post_targets`, `template_targets`,
  `teacher_model_targets` (interface stub only — do not fake), `curated_targets`
- Mode taxonomy:
  - `inner_monologue`
  - `next_anon_reply`
  - `quote_back_reply`     ← directly teaches the gesture wired in chat now
  - `confession`
  - `body_judgement`
  - `image_caption`
  - `dream_fragment`
- Structured user message (matching what chat assembly emits at inference):
  - `BOARD`, `MODE`, `ANON_STATE`, `THREAD_CONTEXT|SESSION_CONTEXT`,
    `RETRIEVED_FRAGMENTS`, `SAFETY_RULES`, `TASK`
- CLI: `--board`, `--mode`, `--limit`, `--out-dir`, `--seed`,
  `--min-fragments`, `--max-fragments`, `--target-strategy`,
  `--include-curated`, `--split`
- Print mode/strategy/safety counts; warn loudly when `template_targets`
  exceeds (e.g.) 30% of total
- Splits: `train.jsonl`, `valid.jsonl`, `test.jsonl`

**Honest dataset architecture choice deferred to Pass 2.** Either:
- (a) Curate ~500–2000 high-quality real-post targets by hand (slow but truthful), or
- (b) Wire `teacher_model_targets` to GPT-4-class API to write assistant outputs against the structured user prompt + retrieved fragments. Costs money but scales.

The shape of the user-message in training **must match** what
`_build_chat_user_prompt` and `thought_grounding._build_user_message` emit at
inference time, otherwise SFT will not transfer cleanly.

## §8 — Eval script (`eval_fit_voice.py`)

**To do:**
- Compare backends side-by-side: template / base mlx / mlx + LoRA
- Fixed test prompt set: mirror, calories, gym machine, confession,
  reply-to-user, dream fragment, image caption, continuing-after-6-turns,
  repeated-topic-anti-loop
- Save JSONL to `var/evals/<timestamp>_<backend>.jsonl` with per-row:
  `eval_id`, `prompt`, `mode`, `backend`, `output`, `runtime_meta`,
  `grounding_fragments`, `validator_result`, `length`, `rejection_reason`,
  `session_state` (if chat-related)
- Manual rubric template (markdown) with rows side-by-side:
  `board_fidelity 0–5`, `corpus_grounding 0–5`, `continuity 0–5`,
  `type_truth 0–5`, `safety pass/fail`, `non_genericness 0–5`,
  `artistic_usefulness 0–5`
- Pull positive examples from `feedback.kept_thoughts()` and
  `feedback.favorite_thought_texts()` to use as rubric anchors

## §6 — Feedback signal as training input

`imageboard_ingestion/feedback.py` (Pass 1) already loads the data — Pass 2
should consume it:
- Eval anchors: kept thoughts + favourites become "ground-truth good" rows in
  the rubric
- Training: kept/favourited outputs as preference-pair positives, skipped as
  negatives. Shape compatible with DPO/ORPO if we ever go beyond SFT.

## §11 — Documentation

Update `README.md` (or new `docs/IMAGEBOARD_LM.md`) covering:
- What "corpus-grounded" means
- Rebuild manifest → profile → retrieval index
- Generate SFT data
- Train MLX LoRA (point at adapter via `IMAGEBOARD_MLX_ADAPTER`)
- Run with template fallback (and the loud warning that surfaces)
- Run eval
- Safety filter scope
- **Why template backend is not the final voice**
- How feedback may feed training later

## Misc Pass-1 TODOs left in code

- `FIT_LOCAL_RUNTIME` deprecation: keep the alias for one release, then drop
  the lookup in `runtime_meta.resolve_backend` and remove the var from
  `settings.py`.
- Sampling: `MonologuePersona.gen_temperature` / `gen_top_p` rows may still
  hold legacy `0.95 / 0.95`. Pass 1 ignores them in chat (env > profile, DB is
  not consulted unless explicitly passed in). Optional one-time `update`
  migration: refresh existing rows to `0.68 / 0.88` for chat-style personas
  and `0.60 / 0.82` for thought-only personas. Not required.
- `_maybe_update_session_summary` runs synchronously and adds an LM call once
  per ~6 turns past turn 11. Acceptable for chat but could become async if
  latency matters more than freshness.

## Acceptance check before starting Pass 2

- Chat at `/` produces coherent, grounded replies on the live MLX backend
- `runtime_meta` is visible in the API responses
- The deprecation warning fires once when `FIT_LOCAL_RUNTIME` is the only var set
- Smoke tests still pass: `pytest djangoscrap/tests/test_imageboard_pass1_smoke.py`
