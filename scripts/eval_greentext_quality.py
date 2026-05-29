"""Score live /greentext/ output against the 6-dimension quality rubric.

Usage:
  .venv/bin/python scripts/eval_greentext_quality.py --n 25

The script:
  1. Pings /api/greentext/reset/ to clear the stream
  2. Pings /api/greentext/tick/ N times to populate
  3. Pings /api/greentext/state/ to fetch what landed
  4. Scores each story on 6 dimensions (heuristics, 0-10 each)
  5. Prints per-story scores + aggregate
  6. Exits 0 if aggregate ≥9.0 on every dim, 1 otherwise

Dimensions and thresholds:
  - narrative_coherence: topic overlap with seed, line variety, single POV
  - punchline_landing:   short last line, punchline-opener token, semantic flip
  - format_fidelity:     every line starts with >, lowercase, fragments
  - voice_authenticity:  /fit/ markers, no coach/therapy register
  - inventiveness:       low overlap with training corpus, seed→body variety
  - repetition_clean:    no repeated phrases, no truncation cliffs
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TRAIN_PATH = ROOT / "training" / "fit_lora" / "data_greentext" / "train.jsonl"


# --- corpus signature index (for inventiveness) --------------------------

def _norm_for_compare(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _load_training_signatures() -> set[str]:
    """Hash of every training story's first-3-lines so we can detect
    memorization."""
    sigs: set[str] = set()
    if not TRAIN_PATH.exists():
        return sigs
    for line in TRAIN_PATH.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        body = ""
        for m in row.get("messages", []):
            if m.get("role") == "assistant":
                body = m.get("content", "")
                break
        if not body:
            continue
        lines = [l for l in body.splitlines() if l.strip().startswith(">")]
        head = " ".join(lines[:5])
        sigs.add(hashlib.md5(_norm_for_compare(head).encode()).hexdigest())
        # Also hash the punchline (last gt line)
        if lines:
            sigs.add(hashlib.md5(_norm_for_compare(lines[-1]).encode()).hexdigest())
    return sigs


# --- scoring functions ----------------------------------------------------

_FIT_MARKERS = (
    "gym", "lift", "bench", "squat", "deadlift", "rack", "bar", "weight",
    "scale", "mirror", "dyel", "mog", "mogged", "mogging", "manlet", "chad",
    "qt", "mirin", "tren", "test", "creatine", "protein", "wagmi", "ngmi",
    "dyel", "skellington", "skinnyfat", "ohp", "rdl", "ab", "abs", "leg",
    "arm", "neck", "jaw", "body", "fat", "lean", "bulk", "cut", "natty",
    "roid", "cycle", "pl8", "sprint", "run", "cardio", "tinder", "bumble",
    "wife", "gf", "girlfriend", "mom", "dad", "girl", "guy",
)

_COACH_PHRASES_RE = re.compile(
    r"\b(?:you can do it|keep going|stay strong|you got this|"
    r"believe in yourself|one step at a time|trust the process|"
    r"hang in there|small wins|keep it up|keep lifting|you'?ll get there|"
    r"i'?m proud of you|you'?re doing great|i'?m here for you|"
    r"don'?t give up|stay positive|love yourself|self-care|"
    r"prioriti[sz]e your)\b",
    re.I,
)

_PUNCHLINE_OPENERS = (
    ">mfw", ">tfw", ">still", ">just", ">kek", ">now", ">i ", ">i'",
    ">nobody", ">fucking", ">fuck", ">guess", ">maybe", ">turns out",
    ">she", ">he", ">it was", ">it's", ">she said", ">he said",
    ">walk home", ">go home", ">go back", ">leave", ">delete",
    # Pass-3 additions from observed /fit/ punchline shapes
    ">oh", ">oh shit", ">oh fuck", ">oh no", ">oh god",
    ">so", ">so much", ">so this is",
    ">wat", ">wat do", ">what now", ">now what", ">what do i",
    ">no", ">not", ">never",
    ">why", ">why am i", ">why is", ">why are",
    ">how", ">how do i", ">how could", ">how is",
    ">my", ">my body", ">my life", ">my mom", ">my wife",
    ">end", ">end up", ">end me",
    ">am i", ">are we", ">is this",
    ">to be", ">to this day", ">today",
    ">there is", ">there was", ">there's", ">theres",
    ">finally", ">eventually", ">decide", ">give up",
    ">delete", ">close", ">stop",
)


def score_narrative_coherence(body: str, seed: str) -> tuple[float, list[str]]:
    """Higher score = more on-topic, consistent POV, single arc."""
    notes = []
    lines = [l.strip() for l in body.splitlines() if l.strip().startswith(">")]
    if len(lines) < 5:
        return 2.0, ["too_few_lines"]
    if len(lines) > 16:
        return 5.0, ["too_many_lines"]

    score = 7.0
    # Topic overlap with seed
    seed_tokens = set(re.findall(r"[a-z]+", seed.lower())) - {"the", "a", "an", "to", "of", "in", "on", "at", "for"}
    body_text = " ".join(lines).lower()
    body_tokens = set(re.findall(r"[a-z]+", body_text))
    if seed_tokens:
        overlap_ratio = len(seed_tokens & body_tokens) / len(seed_tokens)
        if overlap_ratio >= 0.4:
            score += 1.5
        elif overlap_ratio >= 0.2:
            score += 0.5
        else:
            score -= 1.0
            notes.append(f"low_seed_overlap_{overlap_ratio:.2f}")

    # POV consistency (i/my/me vs we/they/he/she)
    first_person = sum(1 for l in lines if re.search(r"\b(?:i|my|me|im|i'?m)\b", l.lower()))
    if first_person / len(lines) >= 0.4:
        score += 0.5  # consistent first-person
    elif first_person / len(lines) < 0.2 and len(lines) >= 6:
        # Could be third-person story (also valid). Don't penalize hard.
        pass

    # Line variety — distinct first words across lines
    first_words = [(l[1:].strip().split() or [""])[0].lower() for l in lines]
    if len(set(first_words)) / len(first_words) < 0.5:
        score -= 1.0
        notes.append("low_line_variety")

    return max(0.0, min(10.0, score)), notes


def score_punchline(body: str) -> tuple[float, list[str]]:
    """Score the last line as a punchline."""
    notes = []
    lines = [l.strip() for l in body.splitlines() if l.strip().startswith(">")]
    if not lines:
        return 0.0, ["no_lines"]
    last = lines[-1].lower()

    score = 4.0  # baseline — last line exists at all
    if len(last) <= 80:
        score += 1.5
    elif len(last) > 140:
        score -= 1.5
        notes.append("punchline_too_long")

    # Punchline opener
    if any(last.startswith(op) for op in _PUNCHLINE_OPENERS):
        score += 2.5
    elif last.endswith("?"):
        score += 2.0
    else:
        score -= 1.0
        notes.append("no_punchline_opener")

    # Semantic flip — last line shouldn't IDENTICALLY repeat setup. BUT if
    # the line starts with a callback opener (>still, >mfw, >tfw, >turns out,
    # >oh shit) AND echoes earlier vocabulary, that IS the callback move —
    # actually positive, not negative.
    head_words = set()
    for l in lines[:3]:
        head_words |= set(re.findall(r"[a-z]+", l.lower())) - {"be", "me", "i", "the", "a", "an", "to", "of", "in"}
    last_words = set(re.findall(r"[a-z]+", last)) - {"be", "me", "i", "the", "a", "an", "to", "of", "in"}
    is_callback_opener = any(last.startswith(op) for op in (
        ">still", ">mfw", ">tfw", ">turns out", ">oh shit", ">oh fuck",
        ">it was", ">it's", ">she said", ">he said", ">to this day"
    ))
    if last_words and head_words:
        overlap = len(head_words & last_words) / len(last_words)
        if is_callback_opener and overlap >= 0.3:
            # Callback move landed
            score += 1.5
        elif overlap >= 0.85:
            # Verbatim echo with no callback move
            score -= 1.0
            notes.append("punchline_echoes_setup")
        elif overlap >= 0.4:
            score += 0.5

    # Single-word `>X` punchline (callback deflation) — also a valid form
    if len(last.split()) <= 2 and len(last) <= 20 and last.startswith(">"):
        score += 1.5

    # Reject moralizing punchlines
    if re.search(r"\b(?:lesson|learned|moral|realized that|now i know|"
                 r"and that'?s why|the point is)\b", last):
        score -= 3.0
        notes.append("moralizing")

    return max(0.0, min(10.0, score)), notes


def score_format_fidelity(body: str) -> tuple[float, list[str]]:
    notes = []
    raw = [l for l in body.splitlines() if l.strip()]
    if not raw:
        return 0.0, ["empty"]
    score = 10.0

    gt = sum(1 for l in raw if l.lstrip().startswith(">"))
    if gt / len(raw) < 1.0:
        score -= 2.0 * (1.0 - gt / len(raw))
        notes.append(f"non_gt_lines_{len(raw)-gt}")

    # Lowercase ratio (in non-marker chars)
    alpha = "".join(c for c in body if c.isalpha())
    if alpha:
        lower = sum(1 for c in alpha if c.islower())
        ratio = lower / len(alpha)
        if ratio < 0.8:
            score -= 1.5
            notes.append(f"low_lowercase_{ratio:.2f}")

    # Line length check
    long_lines = sum(1 for l in raw if len(l) > 180)
    if long_lines:
        score -= 1.0 * long_lines
        notes.append(f"long_lines_{long_lines}")

    # Truncation cliffs (last word too short or ends with comma/conjunction)
    if raw:
        last_tail = raw[-1].rstrip()
        last_word = (last_tail.split() or [""])[-1].rstrip(",;:.").lower()
        if last_tail.endswith((",", "and", "but", "the", "a", "to", "of", "with")) or len(last_word) < 3:
            score -= 1.5
            notes.append("truncation_cliff")

    return max(0.0, min(10.0, score)), notes


def score_voice_authenticity(body: str) -> tuple[float, list[str]]:
    notes = []
    text = body.lower()
    score = 5.0

    markers_present = sum(1 for m in _FIT_MARKERS if re.search(rf"\b{m}\b", text))
    if markers_present >= 4:
        score += 3.0
    elif markers_present >= 2:
        score += 2.0
    elif markers_present >= 1:
        score += 1.0
    else:
        notes.append("no_fit_markers")

    # Punish coach / therapy register
    coach_hits = len(_COACH_PHRASES_RE.findall(text))
    if coach_hits:
        score -= 2.0 * coach_hits
        notes.append(f"coach_phrases_{coach_hits}")

    # Punish "and that's how" / "and that's when" prose endings
    if re.search(r"\band that'?s (?:how|when|why) i\b", text):
        score -= 1.0
        notes.append("prose_ending")

    # Punish "great question" / "you should"
    if re.search(r"\b(?:great question|good question|you should)\b", text):
        score -= 1.5
        notes.append("assistant_register")

    # Punish AI disclosures
    if re.search(r"\b(?:as an ai|i'?m an ai|language model)\b", text):
        score -= 5.0
        notes.append("ai_disclosure")

    return max(0.0, min(10.0, score)), notes


def score_inventiveness(body: str, training_sigs: set[str]) -> tuple[float, list[str]]:
    notes = []
    lines = [l.strip() for l in body.splitlines() if l.strip().startswith(">")]
    if not lines:
        return 0.0, ["empty"]
    head = " ".join(lines[:5])
    head_sig = hashlib.md5(_norm_for_compare(head).encode()).hexdigest()
    if head_sig in training_sigs:
        notes.append("head_matches_training")
        return 3.0, notes
    # Punchline match
    if lines:
        last_sig = hashlib.md5(_norm_for_compare(lines[-1]).encode()).hexdigest()
        if last_sig in training_sigs:
            notes.append("punchline_matches_training")
            return 5.0, notes
    return 9.5, notes


def score_repetition_clean(body: str) -> tuple[float, list[str]]:
    notes = []
    lines = [l.strip() for l in body.splitlines() if l.strip().startswith(">")]
    if len(lines) < 2:
        return 5.0, ["too_few_lines"]
    score = 10.0

    # Within-story duplicate normalized lines
    norms = [_norm_for_compare(l) for l in lines]
    seen = set()
    dupes = 0
    for n in norms:
        if n in seen:
            dupes += 1
        seen.add(n)
    if dupes:
        score -= 2.0 * dupes
        notes.append(f"dup_lines_{dupes}")

    # 2-word prefix collapse (excluding ">be N" ritual openers)
    from collections import Counter
    prefixes: Counter = Counter()
    for l in lines:
        words = re.findall(r"\w+", l.lower())
        if len(words) >= 2 and not (words[0] == "be" and len(words[1]) <= 3):
            prefixes[(words[0], words[1])] += 1
    max_prefix = max(prefixes.values()) if prefixes else 0
    if max_prefix >= 3:
        score -= 3.0
        notes.append(f"prefix_collapse_{max_prefix}")

    # 6-word phrase repetition
    text = " ".join(norms)
    tokens = text.split()
    phrase_counts: Counter = Counter()
    for i in range(len(tokens) - 5):
        phrase = " ".join(tokens[i:i+6])
        phrase_counts[phrase] += 1
    if phrase_counts and max(phrase_counts.values()) >= 2:
        score -= 1.5
        notes.append("phrase_repeat")

    return max(0.0, min(10.0, score)), notes


# --- runner --------------------------------------------------------------

def fetch_state(host: str) -> dict:
    req = urllib.request.Request(f"{host}/api/greentext/state/")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def reset_stream(host: str) -> None:
    req = urllib.request.Request(f"{host}/api/greentext/reset/", method="POST")
    urllib.request.urlopen(req, timeout=15).read()


def trigger_tick(host: str, timeout: float = 240.0) -> dict:
    req = urllib.request.Request(f"{host}/api/greentext/tick/", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def run(host: str, n: int, reset: bool) -> int:
    training_sigs = _load_training_signatures()
    print(f"loaded {len(training_sigs)} training signatures", file=sys.stderr)

    if reset:
        reset_stream(host)
        print("stream reset", file=sys.stderr)

    successes = 0
    attempts = 0
    while successes < n and attempts < n * 3:
        attempts += 1
        try:
            res = trigger_tick(host)
        except Exception as e:
            print(f"  tick err: {e}", file=sys.stderr)
            continue
        if res.get("tick", {}).get("action") == "new_story":
            successes += 1
            print(f"  [{successes}/{n}] generated", file=sys.stderr)
        else:
            print(f"  noop: {res.get('tick', {}).get('reason')}", file=sys.stderr)

    state = fetch_state(host)
    stories = state["stories"]
    print(f"\n{len(stories)} stories on the wall. Scoring...\n", file=sys.stderr)

    scores_by_dim: dict[str, list[float]] = {
        "narrative_coherence": [],
        "punchline_landing": [],
        "format_fidelity": [],
        "voice_authenticity": [],
        "inventiveness": [],
        "repetition_clean": [],
    }
    overall = []
    for i, s in enumerate(stories):
        body = s["body"]
        seed = s.get("seed_topic", "")
        nc, nc_notes = score_narrative_coherence(body, seed)
        pl, pl_notes = score_punchline(body)
        ff, ff_notes = score_format_fidelity(body)
        va, va_notes = score_voice_authenticity(body)
        inv, inv_notes = score_inventiveness(body, training_sigs)
        rep, rep_notes = score_repetition_clean(body)
        story_score = statistics.mean([nc, pl, ff, va, inv, rep])
        overall.append(story_score)
        scores_by_dim["narrative_coherence"].append(nc)
        scores_by_dim["punchline_landing"].append(pl)
        scores_by_dim["format_fidelity"].append(ff)
        scores_by_dim["voice_authenticity"].append(va)
        scores_by_dim["inventiveness"].append(inv)
        scores_by_dim["repetition_clean"].append(rep)

        print(f"=== story {i+1}: {s['post_no']} | {s['anon_variant']} | {s['line_count']} lines ===")
        print(f"seed: {seed[:80]}")
        print(f"body:\n{body}")
        print(f"scores: nc={nc:.1f}{nc_notes or ''}  pl={pl:.1f}{pl_notes or ''}  "
              f"ff={ff:.1f}{ff_notes or ''}  va={va:.1f}{va_notes or ''}  "
              f"inv={inv:.1f}{inv_notes or ''}  rep={rep:.1f}{rep_notes or ''}")
        print(f"overall: {story_score:.2f}\n")

    if not overall:
        print("no stories — fatal", file=sys.stderr)
        return 1

    print("=" * 60)
    print("AGGREGATE")
    print("=" * 60)
    all_pass = True
    for dim, vals in scores_by_dim.items():
        mean = statistics.mean(vals)
        median = statistics.median(vals)
        p25 = sorted(vals)[max(0, len(vals)//4 - 1)]
        line = f"  {dim:25s}  mean={mean:5.2f}  median={median:5.2f}  p25={p25:5.2f}"
        passes = mean >= 9.0
        if not passes:
            all_pass = False
            line += "  <-- FAIL"
        print(line)
    overall_mean = statistics.mean(overall)
    print(f"\n  overall mean: {overall_mean:.2f}  ({'PASS' if all_pass else 'FAIL'})")
    return 0 if all_pass else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="http://localhost:8765")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--no-reset", action="store_true")
    args = p.parse_args()
    sys.exit(run(args.host, args.n, reset=not args.no_reset))


if __name__ == "__main__":
    main()
