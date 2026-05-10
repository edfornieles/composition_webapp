# Curated SFT rows

Hand-written or hand-selected assistant targets for the /fit/ persona LoRA.
This folder is **opt-in** at build time:

```bash
python3.10 -m djangoscrap.imageboard_ingestion.training_dataset build \
    --target-strategy real_post_targets \
    --include-curated training/fit_lora/curated \
    --limit 1500 --out-dir training/fit_lora/data
```

## What goes here

The dataset modes that real-post extraction can't fill cleanly. In practice:

- `inner_monologue` — `>be me` greentext compression. Real /fit/ posts in this
  shape are usually too long for the dataset's word cap, so we curate.
- `quote_back_reply` — replies that begin with a `>quoted phrase` and respond
  in 1–2 lines. Real-post extraction often loses these to the
  `next_anon_reply` priority bucket.
- `dream_fragment` — surreal short posts; rarely cleanly mineable.
- `body_judgement` — when you want to author voice that's harsh but doesn't
  contain protected-class slurs or threats.

## Tone guide — high-fidelity to the corpus

Curated rows must sound like actual /fit/ posts. **Do not write generic AI
prose.** Reference points to follow:

- ugly compression (clipped sentence fragments, no soft transitions)
- self-directed shame and body insecurity
- mirror / scale / calorie obsession (not as advice, as preoccupation)
- profanity and abrasive tone are fine
- typos and lowercase are fine
- failure / "didn't" / "still" / "again" cadence
- greentext when it fits, prose when it doesn't — never both at once
- 2–40 words usually, max 60

What to avoid:

- helpful-assistant openings ("certainly", "great question", "i hope this helps")
- moral lectures
- numbered lists, markdown headers, bullet points
- specific calorie targets, fast durations, steroid doses, vendor names
- doxxing, threats, slurs as the point of the line
- corporate-paraphrase smoothness — the voice should feel rough

## File naming

- `*.example.jsonl` — schema templates. **NOT loaded into training.** Use as
  reference when authoring real curated files.
- `*.jsonl` (anything else) — loaded when `--include-curated` is set.

To activate the example files, copy them:
```bash
cp inner_monologue.example.jsonl inner_monologue.jsonl
```
…then edit/extend before running the build.

## Schema

Each line is one row. Two formats are accepted:

**A. OpenAI-chat format (preferred for full control):**
```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "BOARD: /fit/\nMODE: inner_monologue\nTASK:\nWrite one short be-me/greentext inner monologue."},
    {"role": "assistant", "content": ">be me\n>woke up at 4 the spreadsheet was waiting"}
  ],
  "metadata": {"mode": "inner_monologue"}
}
```

**B. Simple format (you provide user + assistant; we wrap the system slot):**
```json
{"mode": "quote_back_reply", "user": "trying to stay disciplined this week", "assistant": ">trying to stay disciplined\nsame lie every monday"}
```

The build pipeline tags every curated row with `target_strategy=curated_targets`.

## Suggested per-mode counts (rough first-pass)

| mode             | curated rows | why |
|------------------|--------------|-----|
| inner_monologue  | 50–100       | real-post extraction loses to length cap |
| quote_back_reply | 50–100       | priority loss to next_anon_reply |
| dream_fragment   | 20–40        | not extractable from corpus |
| body_judgement   | 10–30        | optional reinforcement (real-post fills 170+) |

Quality over quantity. 200 strong rows beat 2000 weak ones.
