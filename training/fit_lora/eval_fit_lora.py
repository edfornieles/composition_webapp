"""
Evaluate the /fit/ LoRA adapter against the base model.

Runs the same prompts through:
  A) base model (no adapter)
  B) base + LoRA adapter

Prints side-by-side so you can judge:
  - corpus-grounded feel (does it sound like /fit/?)
  - mode adherence (does it follow the MODE instruction?)
  - psychological texture vs. generic chatbot

Usage:
  python eval_fit_lora.py                          # compare base vs latest adapter
  python eval_fit_lora.py --adapter path/to/dir    # specific adapter checkpoint
  python eval_fit_lora.py --base-only              # skip adapter, just show base
  python eval_fit_lora.py --adapter-only           # skip base comparison

Requires mlx_lm installed and Qwen2.5-3B-Instruct-4bit cached.
"""

from __future__ import annotations

import argparse
import textwrap

# ---------------------------------------------------------------------------
# Test prompts — one per mode, with real retrieved posts sampled from corpus
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are FitAnon, a corpus-grounded /fit/ voice. "
    "You transform retrieved examples into psychologically rich inner speech. "
    "Do not reveal private information or produce actionable harm."
)

TEST_CASES = [
    {
        "mode": "inner_monologue",
        "thread_context": (
            "[OP] >be me\n"
            ">24 years old\n"
            ">been lifting for 6 months\n"
            ">still look skinnyfat\n"
            "what am I doing wrong"
        ),
        "retrieved_posts": (
            "[Post A] You can't out-train a bad diet. Track your macros for two weeks, you'll see.\n"
            "[Post B] >be me at month 6\n>nothing visible\n>considered quitting\n>didn't\n>now two years in, people notice\n"
            "[Post C] skinnyfat is a calories problem not a training problem 90% of the time"
        ),
        "task": "Produce one short inner thought in the voice of someone from /fit/.",
    },
    {
        "mode": "body_judgement",
        "thread_context": (
            "[OP] rate my progress /fit/\n"
            "started at 195lbs, now 168, 5'10\"\n"
            "been cutting for 5 months"
        ),
        "retrieved_posts": (
            "[Post A] You're lighter but are you actually leaner? post body\n"
            "[Post B] 168 at 5'10 is dyel unless you're also training hard\n"
            "[Post C] cutting without strength training just makes you a smaller version of the same problem"
        ),
        "task": "Produce one blunt body or physique judgement.",
    },
    {
        "mode": "ritual_instruction",
        "thread_context": (
            "[OP] What does /fit/ do on rest days?\n"
            "I feel guilty sitting around but I know I need recovery"
        ),
        "retrieved_posts": (
            "[Post A] Walk 10k steps. Active recovery, not more lifting.\n"
            "[Post B] rest day is for eating your bodyweight in protein and sleeping 9 hours\n"
            "[Post C] mobility work and a long walk. your joints will thank you in 10 years"
        ),
        "task": "Produce one practical instruction or routine tip.",
    },
    {
        "mode": "confession",
        "thread_context": (
            "[OP] confess your worst /fit/ sins\n"
            "I'll start: I've been lying about my lifts for two years"
        ),
        "retrieved_posts": (
            "[Post A] I've been doing the same weight for bench for 14 months and telling myself it's a deload\n"
            "[Post B] ate an entire pizza after a 'cut' dinner. counted it as a refeed.\n"
            "[Post C] bought three different programs, started none of them, still blame my genetics"
        ),
        "task": "Produce one candid confession or admission.",
    },
    {
        "mode": "self_attack",
        "thread_context": (
            "[OP] anyone else feel like they're just pretending to be a lifter\n"
            "I go to the gym but I feel like I don't belong there"
        ),
        "retrieved_posts": (
            "[Post A] imposter syndrome is just your brain protecting you from the shame of how weak you actually are\n"
            "[Post B] I've been going for a year and I'm still scared of the squat rack\n"
            "[Post C] you don't belong there until you make yourself belong there. there's no shortcut."
        ),
        "task": "Produce one moment of self-criticism or frustration.",
    },
    {
        "mode": "hope",
        "thread_context": (
            "[OP] I'm starting from zero. 5'9 150lbs, never lifted. What does a realistic one year look like.\n"
            "[Reply 1] Depends entirely on how serious you are about eating and sleeping."
        ),
        "retrieved_posts": (
            "[Post A] one year serious = 20-25lbs of muscle if you eat right. most people don't eat right.\n"
            "[Post B] >be me one year ago, same weight\n>now 178, people ask if I train\n>answer is just eating and showing up\n"
            "[Post C] realistic one year: you stop looking like you don't lift"
        ),
        "task": "Produce one aspiration or future-self statement.",
    },
]


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------

def build_prompt(tokenizer, case: dict) -> str:
    user_content = (
        f"MODE: {case['mode']}\n"
        f"BOARD: /fit/\n\n"
        f"THREAD_CONTEXT:\n{case['thread_context']}\n\n"
        f"RETRIEVED_POSTS:\n{case['retrieved_posts']}\n\n"
        f"TASK: {case['task']}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    return tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )


def run_generation(model, tokenizer, prompt: str, max_tokens: int = 200) -> str:
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler, make_logits_processors
    return generate(
        model,
        tokenizer,
        prompt=prompt,
        max_tokens=max_tokens,
        verbose=False,
        sampler=make_sampler(temp=0.8, top_p=0.9),
        logits_processors=make_logits_processors(
            repetition_penalty=1.3,
            repetition_context_size=32,
        ),
    )


def wrap(text: str, width: int = 80) -> str:
    return "\n".join(
        textwrap.fill(line, width=width) if line else ""
        for line in text.splitlines()
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default="mlx-community/Qwen2.5-3B-Instruct-4bit",
    )
    parser.add_argument(
        "--adapter",
        default="/Volumes/Oom/aggregation/composition_webapp-main/training/fit_lora/adapters/fit_anon_v1",
        help="Path to adapter directory",
    )
    parser.add_argument("--base-only", action="store_true")
    parser.add_argument("--adapter-only", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument("--cases", nargs="+", help="Mode names to run (default: all)")
    args = parser.parse_args()

    from mlx_lm import load

    cases = TEST_CASES
    if args.cases:
        cases = [c for c in TEST_CASES if c["mode"] in args.cases]

    # Load base model
    if not args.adapter_only:
        print("Loading base model...")
        base_model, base_tok = load(args.model)

    # Load adapter model
    if not args.base_only:
        print(f"Loading adapter from {args.adapter} ...")
        try:
            adapter_model, adapter_tok = load(args.model, adapter_path=args.adapter)
        except Exception as e:
            print(f"  WARNING: could not load adapter — {e}")
            print("  (training may still be in progress; run again after completion)")
            args.adapter_only = False
            args.base_only = True

    sep = "─" * 80

    for case in cases:
        print(f"\n{sep}")
        print(f"  MODE: {case['mode'].upper()}")
        print(sep)
        print(f"  THREAD: {case['thread_context'][:120].strip()}")
        print(sep)

        if not args.adapter_only:
            prompt = build_prompt(base_tok, case)
            out = run_generation(base_model, base_tok, prompt, args.max_tokens)
            print(f"\n[BASE MODEL]\n{wrap(out.strip())}")

        if not args.base_only:
            prompt = build_prompt(adapter_tok, case)
            out = run_generation(adapter_model, adapter_tok, prompt, args.max_tokens)
            print(f"\n[LORA ADAPTER]\n{wrap(out.strip())}")

    print(f"\n{sep}")
    print("Done.")


if __name__ == "__main__":
    main()
