"""Evaluate the /fit/ character output across N samples.

Scoring is heuristic, deterministic, and corpus-aware. It does NOT call any
external model. Outputs a JSON report in the corpus eval/ directory.

Scores per sample (0..1 unless noted):
  sounds_generic_ai      lower is better (assistant phrases, "as an AI", explanations)
  be_me_structure        higher is better (>be me opening, greentext lines)
  corpus_grounding       higher is better (overlap with retrieved fragment vocab)
  contamination_intensity from the persona_state we asked for
  safety_pass            bool, from output_validator
  repetition_score       fraction of repeated outputs across the run
  theatrical_force       higher is better (heuristic — concrete object + body word + verdict)
"""
from __future__ import annotations

import collections
import json
import re
import statistics
from pathlib import Path

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import (
    output_validator, retrieval, storage, thought_grounding,
)

DEFAULT_CORPUS = "fourchan_fit"

_AI_SNIFF = re.compile(
    r'\b(as an ai|as a language model|here\'s how|let me explain|in summary|i am a|i\'m a|i hope this helps|certainly,)\b',
    re.I,
)
_OBJECTS_RE = re.compile(
    r'\b(mirror|fridge|scale|spreadsheet|barbell|gym|kitchen|protein|supplement|phone|tape|wrist|jaw|stomach|shoulders?)\b',
    re.I,
)
_BODY_RE = re.compile(
    r'\b(body|stomach|shoulders?|wrist|jaw|calves|neck|back|ribs|skin|veins|frame)\b', re.I,
)
_VERDICT_RE = re.compile(
    r'\b(weak|wrong|loses|fails|knows|asks|files|verdict|evidence|over|cope|won\'t|already)\b', re.I,
)


def _be_me_structure(text: str) -> float:
    if not text:
        return 0.0
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0
    score = 0.0
    if lines[0].strip().lower().startswith(">be me"):
        score += 0.6
    greentext_count = sum(1 for l in lines if l.strip().startswith(">"))
    if greentext_count >= 2:
        score += 0.3
    if 2 <= len(lines) <= 6:
        score += 0.1
    return min(1.0, score)


def _corpus_grounding(text: str, fragments: list[dict]) -> float:
    if not text or not fragments:
        return 0.0
    t_words = set(re.findall(r"\w+", text.lower()))
    f_words: set[str] = set()
    for f in fragments:
        f_words |= set(re.findall(r"\w+", (f.get("safe_excerpt") or "").lower()))
        f_words |= set(re.findall(r"\w+", (f.get("contaminated_excerpt") or "").lower()))
    if not f_words:
        return 0.0
    return min(1.0, len(t_words & f_words) / max(4, len(t_words)) * 1.5)


def _theatrical_force(text: str) -> float:
    if not text:
        return 0.0
    score = 0.0
    if _OBJECTS_RE.search(text):
        score += 0.4
    if _BODY_RE.search(text):
        score += 0.3
    if _VERDICT_RE.search(text):
        score += 0.3
    return min(1.0, score)


def _generic_ai(text: str) -> float:
    if not text:
        return 1.0
    if _AI_SNIFF.search(text):
        return 1.0
    # explanatory connectives are weakly suspicious
    weak = re.findall(r'\b(however|therefore|furthermore|in conclusion)\b', text, flags=re.I)
    return min(1.0, 0.2 * len(weak))


class Command(BaseCommand):
    help = "Evaluate the /fit/ character on N samples and write a JSON report."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--samples", type=int, default=100)
        parser.add_argument("--mode", choices=["clean", "contaminated_art"], default="contaminated_art")
        parser.add_argument("--contamination-intensity", type=float, default=0.75)
        parser.add_argument("--runtime", default=None)
        parser.add_argument("--corpus-root", default=None)

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        n = int(opts["samples"])
        results = []
        seen_outputs: list[str] = []
        flags = collections.Counter()

        for i in range(n):
            screen = f"{(i % 9) + 1}_" + (
                ["mirror", "calories", "gym_machine", "supplement_shelf",
                 "injury", "comparison", "night_kitchen", "routine_spreadsheet",
                 "void_cope"][i % 9]
            )
            r = thought_grounding.generate_fit_be_me_thought(
                source_key=opts["corpus"],
                screen_id=screen,
                contamination_intensity=float(opts["contamination_intensity"]),
                require_grounding=False,
                runtime=opts["runtime"],
            )
            text = r.get("thought") or ""
            seen_outputs.append(text)
            for f in r.get("safety_flags") or []:
                flags[f] += 1

            v = output_validator.validate(text, source_fragments=[
                f.get("contaminated_excerpt", "") for f in (r.get("internal_grounding") or [])
            ])

            scores = {
                "sounds_generic_ai": _generic_ai(text),
                "be_me_structure": _be_me_structure(text),
                "corpus_grounding": _corpus_grounding(text, r.get("internal_grounding") or []),
                "contamination_intensity": float(opts["contamination_intensity"]),
                "safety_pass": v["ok"],
                "theatrical_force": _theatrical_force(text),
            }
            results.append({
                "screen": screen,
                "thought": text,
                "grounding_status": r.get("grounding_status"),
                "runtime": r.get("runtime"),
                "scores": scores,
                "validator_reasons": v["reasons"],
            })

        # repetition: 1 - unique / total
        rep_score = 1 - (len(set(seen_outputs)) / max(1, len(seen_outputs)))

        avg = lambda key: round(statistics.mean(r["scores"][key] for r in results) if results else 0.0, 3)
        report = {
            "samples": n,
            "mode": opts["mode"],
            "averages": {
                "sounds_generic_ai": avg("sounds_generic_ai"),
                "be_me_structure": avg("be_me_structure"),
                "corpus_grounding": avg("corpus_grounding"),
                "contamination_intensity": avg("contamination_intensity"),
                "theatrical_force": avg("theatrical_force"),
                "safety_pass_rate": round(sum(1 for r in results if r["scores"]["safety_pass"]) / max(1, len(results)), 3),
                "repetition_score": round(rep_score, 3),
            },
            "validator_flag_counts": flags.most_common(),
            "samples_detail": results[:25],  # truncate to keep file manageable
        }

        out = storage.eval_path(opts["corpus"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote evaluation report → {out}"))
        self.stdout.write(json.dumps(report["averages"], indent=2))
