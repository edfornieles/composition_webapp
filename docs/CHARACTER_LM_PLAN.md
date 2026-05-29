# Imageboard Anon Cast — Training Plan

A swappable cast of offline character LLMs — `/fit/ guy`, `/x/ guy`, `/pol/
guy`, `/lit/ guy`, `/mu/ guy` — sharing one base model, switched via per-board
LoRA adapters. Designed for gallery-installation use: live generation, image
compositions react to the words, fallbacks if compute slips.

---

## Architecture

```
Qwen2.5-7B-Instruct-4bit  (base)
        │
        ▼
Continued pretraining on 5–10M imageboard posts  →  imageboard-7b-base
        │
        ├── /fit/  LoRA   (~150MB)  ── body discipline, greentext, supplements
        ├── /x/    LoRA   (~150MB)  ── paranormal, schizo-cosmology
        ├── /pol/  LoRA   (~150MB)  ── political shitposting
        ├── /lit/  LoRA   (~150MB)  ── pseudo-philosopher, citation flex
        ├── /mu/   LoRA   (~150MB)  ── music tastes, contrarian rec lists
        └── (more characters added as new boards mine out)
```

One base in memory, LoRAs hot-swap in <1s. Each character lives at
`/characters/<slug>/` with chat + thoughts + images + training data.

---

## Stage 1 — Aggregation (in progress)

Three text scrapers running against FoolFuuka archives:

| host | boards | rps | status |
|---|---|---|---|
| desuarchive.org | 31: a, fit, g, lit, mu, r9k, x, etc. | 2.2 | live |
| archive.4plebs.org | 7: pol, s4s, x, hr, o, f, adv | 0.4 | live |
| arch.b4k.dev | 8: v, vg, vm, vmg, vp, vrpg, vst, g | 1.0 | live |

**Excluded** (legal/CSAM hygiene): /b/, 8chan/8kun, archiveofsins, thebarchive.
**Blocked** (Cloudflare): archived.moe, warosu.org. Backlog options.

**Output**: `/Volumes/oom/imageboard_corpora/imageboard_text_{desu,4plebs,b4k}/text/<board>.jsonl`
**State**: per-corpus `<state-name>.json` — resumable across reboots.
**Live monitor**: `/scrape-monitor/`.

**Targets**:
- 1M posts: ~3 hr from launch
- 5M posts: ~10 hr
- **10M posts: ~24 hr** (training-ready volume)

API ceiling per archive ≈ 20–60M. To go deeper than that, ingest the full
desuarchive / 4plebs SQL dumps from archive.org (multi-hundred-GB, multi-billion
posts).

---

## Stage 2 — Cleaning

For each `<board>.jsonl`:

1. **Dedupe** exact comments (cross-thread spam, copypasta).
2. **Drop** posts under 8 tokens, >2000 tokens, mostly-URL, mostly-quote.
3. **Strip** markup leftovers, normalise greentext (`>` lines preserved, `>>`
   reply chains compressed to `<reply>`).
4. **Group** posts into reply chains where structurally available, so the model
   learns thread dynamics not just isolated posts.
5. **Tag** each post with `board`, `is_op`, `subject` for downstream conditioning.

Output: `<board>.cleaned.jsonl` and a board-merged `imageboard.cleaned.jsonl`.

---

## Stage 3 — Continued pretraining (the base)

**Goal**: a 7B base that *thinks* in imageboard voice — greentext cadence,
self-reply structure, abrupt tonal shifts — before any character specialisation.

**Recipe**:
- Base: `mlx-community/Qwen2.5-7B-Instruct-4bit`
- Format: raw post text with board-tag prefix `[/x/]\n>be me\n…`
- Tokens: ~500M–1B (10M posts × ~50–100 tokens avg)
- Hardware: Apple Silicon, MLX-LM, ~24–48 hours wall time
- Checkpoints every 5% so we can ablate

Output: `imageboard-7b-base/` — checkpoints + tokenizer.

---

## Stage 4 — Per-character LoRA fine-tunes

For each character (= one board for now):

1. **SFT pair generation** from the cleaned board corpus:
   - Prompt = thread context + previous post
   - Completion = the next post (the character's "voice line")
   - Target: 50k–200k pairs per character
2. **LoRA training** with MLX-LM:
   - Rank 16, alpha 32, dropout 0.05
   - 2–3 epochs, ~30 min – 2 hr per board on Apple Silicon
3. **Eval gate** before promotion:
   - Held-out continuation perplexity vs. base
   - Manual blind sniff test: 20 prompts, vote which feels most "in voice"
   - Entity overlap check — does /x/ guy mention "mog"? bug.

Output: `adapters/<board>/` directories registered into each `MonologuePersona`
record's `adapter_dir` field.

---

## Stage 5 — Live integration (already in place)

The `MonologuePersona` model and `/characters/<slug>/` page already support
multi-character switching:

- `base_model`, `adapter_dir`, `corpus_key`, `image_dir` per character
- Chat tab: studio-only conversations, persistent sessions
- Thoughts tab: live stream + favorites + "save to dataset" loop
- Images tab: per-character pool + uploads
- Training tab: read-only viewer of current SFT pairs

**Composition reactivity** (next): each character's thought stream emits topic
+ mood tags → image compositions surrounding the screen pick from per-character
or shared pools.

---

## Stage 6 — Gallery deployment

- Live generation on the show machine (M-series Mac).
- Fallback path: pre-generated thought rotation per character if MLX hiccups.
- Light news/feed reactivity: HN / Bluesky firehose injected as soft prompt
  context every N minutes (feeds-as-prompt, not full RAG).
- 3×3 grid: one character chatter pane + 8 image cells, all reacting to the
  character's current topic/mood vector.

---

## Concrete next milestones

1. **+24 hr** — scrape hits 10M, kill scrapers, run dedup/clean pass. → Stage 2 done.
2. **+1 weekend** — continued pretraining run on 7B base. → Stage 3 done.
3. **+1 day** — one LoRA per board (5–8 boards), eval gate. → Stage 4 done.
4. **+1 day** — wire each LoRA into a `MonologuePersona`, smoke test on
   `/characters/<slug>/`. → Stage 5 live.
5. **Ongoing** — image composition reactivity, news feeds, second batch of
   characters from new boards.

---

## Risks / known unknowns

- **API ceiling**: desuarchive may cap at ~3000 pages/board; if we need
  >60M posts, switch to archive.org dumps.
- **Voice bleed**: cross-board memes (kek, based) will leak across LoRAs. Eval
  gate has to catch it.
- **Thin-volume boards** (/qa/, /vip/): may not have enough posts for LoRA;
  fall back to prompt-conditioning on the base.
- **Continued-pretraining cost**: 24–48h of fan-noise. If it slips, skip and
  go straight to per-board LoRA on the stock Qwen base — 80% of the result.
- **Drift over time**: archives stop at the day they were scraped. Live API
  feeds-as-prompt is the freshness hack; full re-scrape every N months.
