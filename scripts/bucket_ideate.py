#!/usr/bin/env python3
"""Identity-to-bucket-specs ideation via LLM.

Given an identity / aesthetic concept ("goth", "millennial parent",
"crypto bro 2021", "diaspora teenage girl 2008"), returns a list of
BucketSpecs covering its visual world. Each spec includes a ruscha_rule,
include/exclude terms, and a starter source plan.

The user reviews the suggestions, edits/removes/adds, and then runs
bucket_build.py on whichever specs they accept.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/bucket_ideate.py \\
        --identity "goth identity" \\
        --n 12 \\
        --out scripts/specs/goth/

Without API key set, prints the prompt that would be sent so you can run
it manually in claude.ai or chat.openai.com and paste the JSON back into
scripts/specs/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bucket_spec import BucketSpec, SourceConfig

SYSTEM_PROMPT = """You are an aesthetic-curator. Given an identity or cultural
concept, you propose visual buckets that, together, make up the media
environment of someone living that identity. Each bucket is one
aesthetically-coherent slice of imagery (1,000–2,000 images would fit in it).

Apply the Ed Ruscha rule: each bucket's images obey ONE structural
constraint. e.g. "all images are wide-shot photographs of dense forests
in low light, no people, no buildings" — not a vague mood-board.

For each bucket return a strict-JSON object with:
  name             snake_case folder name, prefix with the identity slug
  category         broad category (the identity)
  subcategory      narrower descriptor
  ruscha_rule      one sentence, single constraint
  include_terms    5–10 search terms / phrases
  exclude_terms    things to filter out
  source_suggestions: list of objects with `type` (one of: reddit,
                  wikimedia, gallery_dl, yandex_reverse, lora_generate)
                  and `params` describing what to fetch. For reddit,
                  include `subreddits`. For wikimedia, include
                  `categories`. For gallery_dl, include `urls`.
  notes            anything you want the human to know

Return a JSON array of 8–16 objects. No prose outside the JSON.
"""


def build_user_prompt(identity: str, n: int) -> str:
    return f"""Identity: "{identity}"

Propose {n} visual buckets covering the media environment of someone living
this identity. Stay specific. Avoid generic "moodboard" buckets. Mix:
  - core landscape / environment buckets
  - body / fashion buckets
  - object / fetish buckets
  - architectural / interior buckets
  - photography-style buckets
  - some surprising adjacent buckets the human would not have thought of

Return strict JSON only.
"""


def call_anthropic(system: str, user: str, *, model: str = "claude-haiku-4-5-20251001") -> str:
    """Cheap structured-output call. Falls back to printing prompt if no key."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("=" * 70, flush=True)
        print("ANTHROPIC_API_KEY not set. Run this prompt manually:", flush=True)
        print("=" * 70, flush=True)
        print("SYSTEM:\n" + system + "\n", flush=True)
        print("USER:\n" + user, flush=True)
        print("=" * 70, flush=True)
        return ""
    try:
        import anthropic
    except ImportError:
        sys.exit("pip install anthropic")
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


def parse_specs(raw: str, identity_slug: str) -> list[BucketSpec]:
    # Tolerate any extra prose around the JSON array.
    raw = raw.strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("No JSON array in LLM output.")
    arr = json.loads(raw[start: end + 1])
    specs: list[BucketSpec] = []
    for item in arr:
        sources_raw = item.get("source_suggestions") or item.get("sources") or []
        sources = [SourceConfig(type=s["type"], params=s.get("params", {})) for s in sources_raw]
        specs.append(BucketSpec(
            name=item["name"],
            category=item.get("category", identity_slug),
            subcategory=item.get("subcategory", ""),
            target_count=int(item.get("target_count", 2000)),
            ruscha_rule=item.get("ruscha_rule", ""),
            include_terms=list(item.get("include_terms", [])),
            exclude_terms=list(item.get("exclude_terms", [])),
            sources=sources,
            notes=item.get("notes", ""),
        ))
    return specs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--identity", required=True)
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--out", required=True, help="Directory to write spec JSONs.")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    args = ap.parse_args()

    identity_slug = args.identity.lower().replace(" ", "_").replace("/", "_")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    user = build_user_prompt(args.identity, args.n)
    raw = call_anthropic(SYSTEM_PROMPT, user, model=args.model)
    if not raw:
        sys.exit("(no API key — paste prompt above into a chat, save the JSON array, then re-run with --from-file)")

    specs = parse_specs(raw, identity_slug)
    for s in specs:
        path = out_dir / f"{s.name}.json"
        path.write_text(json.dumps(s.to_dict(), indent=2))
        print(f"  wrote {path}", flush=True)

    index_path = out_dir / "_index.json"
    index_path.write_text(json.dumps({
        "identity": args.identity,
        "specs": [s.name for s in specs],
        "model": args.model,
    }, indent=2))
    print(f"\n{len(specs)} specs in {out_dir}. Review, edit, then build with bucket_build.py.", flush=True)


if __name__ == "__main__":
    main()
