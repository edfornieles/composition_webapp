"""
Standalone FitAnon chat server.

Loads Qwen2.5-3B-Instruct-4bit + the fit_anon_v1 LoRA adapter and serves
a minimal chat UI at http://localhost:8765.

Usage:
    python training/fit_lora/chat_server.py
    python training/fit_lora/chat_server.py --no-adapter   # base model only
    python training/fit_lora/chat_server.py --port 9000
    python training/fit_lora/chat_server.py --adapter path/to/other/adapter

The page has:
  - A message input
  - A mode selector (inner_monologue / reply_like_thread / confession / …)
  - The conversation history
  - A "retrieved posts" panel showing what was used to ground the reply

Model loads on first request and stays in RAM.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CORPUS_PATH = Path(
    os.environ.get(
        "FIT_CORPUS_PATH",
        "/Volumes/Oom/imageboard_corpora/imageboard_text_desu/text/fit.jsonl",
    )
)
DEFAULT_ADAPTER = str(
    Path(__file__).parent / "adapters" / "fit_anon_v1"
)
DEFAULT_MODEL = "mlx-community/Josiefied-Qwen2.5-7B-Instruct-abliterated-v2-4-bit"

SYSTEM_PROMPT = (
    "You are FitAnon, a corpus-grounded /fit/ voice. "
    "You transform retrieved examples into psychologically rich inner speech. "
    "Reply terse, 1–4 lines, greentext where it fits. "
    "Do not reveal private information or produce actionable harm."
)

MODES = [
    "reply_like_thread",
    "inner_monologue",
    "confession",
    "body_judgement",
    "hope",
    "self_attack",
    "ritual_instruction",
    "product_review",
]

MODE_TASK = {
    "inner_monologue": "Produce one short inner thought in the voice of someone from /fit/.",
    "confession": "Produce one candid confession or admission.",
    "body_judgement": "Produce one blunt body or physique judgement.",
    "hope": "Produce one aspiration or future-self statement.",
    "self_attack": "Produce one moment of self-criticism or frustration.",
    "ritual_instruction": "Produce one practical instruction or routine tip.",
    "product_review": "Produce one short product or supplement take.",
    "reply_like_thread": "Reply naturally as the next anon in this thread.",
}

# ---------------------------------------------------------------------------
# Corpus index (loaded once, used for retrieval)
# ---------------------------------------------------------------------------

_corpus_posts: list[dict] = []


def _load_corpus() -> None:
    global _corpus_posts
    if _corpus_posts:
        return
    if not CORPUS_PATH.exists():
        print(f"  Corpus not found at {CORPUS_PATH} — retrieval disabled")
        return
    print(f"  Loading corpus for retrieval from {CORPUS_PATH}...")
    with open(CORPUS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                comment = (row.get("comment") or "").strip()
                if len(comment) >= 60:
                    _corpus_posts.append(row)
            except json.JSONDecodeError:
                pass
    print(f"  Loaded {len(_corpus_posts):,} posts for retrieval")


def _retrieve(query: str, top_k: int = 6) -> list[dict]:
    if not _corpus_posts:
        return []
    # Simple keyword overlap retrieval (no embeddings needed for this demo)
    query_words = set(re.sub(r"[^\w\s]", " ", query.lower()).split())
    query_words.discard("")
    if not query_words:
        return random.sample(_corpus_posts, min(top_k, len(_corpus_posts)))
    scored: list[tuple[int, dict]] = []
    sample = random.sample(_corpus_posts, min(2000, len(_corpus_posts)))
    for post in sample:
        text = (post.get("comment") or "").lower()
        score = sum(1 for w in query_words if w in text)
        if score > 0:
            scored.append((score, post))
    scored.sort(key=lambda x: -x[0])
    top = [p for _, p in scored[:top_k]]
    if len(top) < top_k:
        extra = random.sample(_corpus_posts, min(top_k - len(top), len(_corpus_posts)))
        top += extra
    return top[:top_k]


# ---------------------------------------------------------------------------
# Model (loaded on first request)
# ---------------------------------------------------------------------------

_model_cache: dict = {}


def _load_model(model_id: str, adapter_path: str | None) -> tuple:
    key = (model_id, adapter_path)
    if key not in _model_cache:
        from mlx_lm import load
        print(f"  Loading {model_id}" + (f" + adapter {adapter_path}" if adapter_path else " (base)"))
        kwargs = {}
        if adapter_path and Path(adapter_path).exists():
            kwargs["adapter_path"] = adapter_path
        model, tokenizer = load(model_id, **kwargs)
        _model_cache[key] = (model, tokenizer)
        print("  Model ready.")
    return _model_cache[key]


def _generate(
    model,
    tokenizer,
    messages: list[dict],
    max_tokens: int = 160,
) -> str:
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler, make_logits_processors

    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    out = generate(
        model,
        tokenizer,
        prompt=prompt,
        max_tokens=max_tokens,
        verbose=False,
        sampler=make_sampler(temp=0.85, top_p=0.92),
        logits_processors=make_logits_processors(
            repetition_penalty=1.3,
            repetition_context_size=32,
        ),
    )
    return str(out).strip()


# ---------------------------------------------------------------------------
# Chat logic
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r">>\d+\n?", "", text)
    return text.strip()


def build_messages(
    user_msg: str,
    history: list[dict],
    mode: str,
    retrieved: list[dict],  # kept for UI display, not injected into prompt
) -> list[dict]:
    """Build a proper multi-turn message list.

    Retrieval is NOT included in the prompt — at 600–3k training iters the
    LoRA hasn't learned to treat retrieved posts as style-only context; they
    hijack the response. They still appear in the UI panel for transparency.
    The conversation history is real alternating turns for proper context tracking.
    """
    mode_hint = ""
    if mode == "inner_monologue":
        mode_hint = " Use greentext >be me style."
    elif mode == "confession":
        mode_hint = " Confess something, self-aware tone."
    elif mode == "body_judgement":
        mode_hint = " Give a blunt physical assessment."
    elif mode == "ritual_instruction":
        mode_hint = " Give one concrete routine or nutrition tip."
    elif mode == "self_attack":
        mode_hint = " Self-deprecating, frustrated tone."
    elif mode == "hope":
        mode_hint = " Express a goal or aspiration."

    system = (
        "You are an anonymous poster on 4chan /fit/. "
        "Terse. Crude. Dry. Blunt. "
        "Use > for greentext lines. "
        "NEVER repeat or paraphrase what was just said — react to it. "
        "Add your judgement, continue the story, or ask one sharp question. "
        "No warmth, no therapy, no encouraging tone. "
        "Examples of style:\n"
        ">2 days on floor\n>ngmi\n\n"
        ">back like nothing happened\n>respect honestly\n\n"
        ">floor is where gains are made\n>or lost, either way"
        + mode_hint
    )

    messages: list[dict] = [{"role": "system", "content": system}]

    for m in history[-8:]:
        role = "user" if m["role"] == "user" else "assistant"
        messages.append({"role": role, "content": m["content"][:500]})

    messages.append({"role": "user", "content": user_msg})
    return messages


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

_args: argparse.Namespace | None = None


@app.route("/")
def index():
    return render_template_string(HTML_PAGE, modes=MODES)


@app.route("/api/status")
def api_status():
    adapter = None if _args.no_adapter else _args.adapter
    return jsonify({
        "model": _args.model.split("/")[-1],
        "adapter": Path(adapter).name if adapter else None,
        "loaded": bool(_model_cache),
    })


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(force=True) or {}
    user_msg = (body.get("message") or "").strip()[:1000]
    mode = body.get("mode") or "reply_like_thread"
    history = body.get("history") or []  # [{role, content}]

    if not user_msg:
        return jsonify({"error": "empty message"}), 400

    if mode not in MODES:
        mode = "reply_like_thread"

    retrieved = _retrieve(user_msg, top_k=6)
    retrieved_snippets = [
        _clean(p.get("comment", ""))[:300] for p in retrieved
    ]

    messages = build_messages(user_msg, history, mode, retrieved)

    try:
        model, tokenizer = _load_model(_args.model, None if _args.no_adapter else _args.adapter)
        reply = _generate(model, tokenizer, messages, max_tokens=80)
    except Exception as e:
        reply = f"[generation error: {e}]"

    return jsonify({
        "reply": reply,
        "mode": mode,
        "retrieved": retrieved_snippets,
    })


# ---------------------------------------------------------------------------
# Wall — continuous greentext thread
# ---------------------------------------------------------------------------

# Seed posts to kick off the thread if it's empty
_WALL_SEEDS = [
    ">be me\n>23\n>haven't left the house in 4 days",
    ">be me\n>finally hit 1/2/3/4\n>nobody to tell",
    ">be me\n>eating protein shake for the third meal in a row\n>actually prefer it now",
    ">be me\n>look in mirror\n>still not where i want to be\n>close the blinds",
    ">be me\n>alarm goes off at 5am\n>gym opens in 10\n>lie there anyway",
]

# Distinct anon voice seeds — vary which one is "speaking" each post
_ANON_VOICES = [
    "You are an anonymous /fit/ poster mid-story. Continue the thread with 2-4 greentext lines. Different angle from the last post.",
    "You are a different anon replying to the thread. Add one short observation or continue the narrative. Greentext only.",
    "You are the original anon, continuing your own story. 2-3 greentext lines. Move it forward.",
    "You are a lurker who just has to comment. Dry, one or two lines, greentext.",
    "You are an anon who's been through this. Continue with a brief parallel or judgement. Greentext.",
]


@app.route("/wall")
def wall():
    return render_template_string(WALL_PAGE)


_SCENE_SHIFTS = [
    ">be me\n>different day now",
    ">fast forward",
    ">meanwhile",
    ">next morning",
    ">week later",
    ">cut to gym",
    ">somewhere else entirely",
]

_TOPIC_POOLS = [
    "gym, lifting, gains, failure",
    "eating, diet, calories, protein, late night",
    "sleep, exhaustion, floor, bed, ceiling",
    "social failure, isolation, the chair",
    "the body, the mirror, the scale",
    "work, class, pretending to be normal",
    "hope, goal, maybe one day",
]


def _detect_repetition(posts: list[str], window: int = 3) -> str | None:
    """Return the repeated opening phrase if the last `window` posts share one."""
    if len(posts) < window:
        return None
    snippets = []
    for p in posts[-window:]:
        first_line = next((ln.strip() for ln in p.splitlines() if ln.strip()), "")
        # first 3 words of the first greentext line
        words = first_line.lstrip(">").strip().split()[:3]
        snippets.append(" ".join(words).lower())
    if len(set(snippets)) == 1:
        return snippets[0]
    # also catch if 2 of 3 start the same
    if snippets[-1] == snippets[-2]:
        return snippets[-1]
    return None


@app.route("/api/wall/next", methods=["POST"])
def api_wall_next():
    body = request.get_json(force=True) or {}
    recent_posts = body.get("posts") or []

    if not recent_posts:
        seed = random.choice(_WALL_SEEDS)
        return jsonify({"post": seed, "post_num": _rand_post_num()})

    repeated = _detect_repetition(recent_posts)
    scene_break = repeated is not None

    # Build context — only last 4 posts to reduce echo-chamber effect
    context = "\n\n".join(p[:300] for p in recent_posts[-4:])

    # Collect forbidden opening phrases from recent posts
    forbidden = set()
    for p in recent_posts[-3:]:
        first = next((ln.strip().lstrip(">").strip() for ln in p.splitlines() if ln.strip()), "")
        if first:
            forbidden.add(first[:40].lower())

    topic = random.choice(_TOPIC_POOLS)
    voice = random.choice(_ANON_VOICES)

    if scene_break:
        shift = random.choice(_SCENE_SHIFTS)
        scene_instruction = (
            f"The thread is stuck. Break the loop. Start completely fresh.\n"
            f"Use this transition: {shift}\n"
            f"New topic area: {topic}."
        )
    else:
        scene_instruction = f"New angle. Topic area: {topic}."

    forbidden_note = ""
    if forbidden:
        forbidden_note = (
            "\nDo NOT start with any of these phrases: "
            + "; ".join(f'"{f}"' for f in list(forbidden)[:4])
        )

    system = (
        "You are an anonymous poster on 4chan /fit/. "
        "Every line starts with >. Terse. Dry. No warmth. No disclaimers. "
        "NEVER copy or continue the exact phrasing of recent posts."
    )
    user_turn = (
        f"{voice}\n\n"
        f"Thread so far:\n{context}\n\n"
        f"{scene_instruction}{forbidden_note}\n\n"
        "Write the next post. 2-4 greentext lines only."
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_turn},
    ]

    try:
        model, tokenizer = _load_model(_args.model, None if _args.no_adapter else _args.adapter)
        post = _generate(model, tokenizer, messages, max_tokens=100)
        lines = [ln for ln in post.splitlines() if ln.strip().startswith(">")]
        post = "\n".join(lines) if lines else post
    except Exception as e:
        post = f">generation error\n>{e}"

    return jsonify({"post": post.strip(), "post_num": _rand_post_num()})


def _rand_post_num() -> str:
    return str(random.randint(10_000_000, 99_999_999))


# ---------------------------------------------------------------------------
# HTML / JS page
# ---------------------------------------------------------------------------

WALL_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>/fit/ — live thread</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:      #0f1117;
    --surface: #17191f;
    --border:  #252830;
    --text:    #b8bcc8;
    --dim:     #44495a;
    --green:   #789922;
    --num:     #555d72;
    --font:    "SF Mono", "Fira Mono", "Consolas", monospace;
    --serif:   Georgia, "Times New Roman", serif;
  }
  html, body { height: 100%; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--serif);
    font-size: 14px;
    line-height: 1.6;
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .board-header {
    width: 100%;
    max-width: 860px;
    padding: 18px 20px 10px;
    text-align: center;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .board-header h1 {
    font-family: var(--font);
    font-size: 13px;
    letter-spacing: 0.12em;
    color: var(--dim);
    text-transform: uppercase;
  }
  .board-header .thread-title {
    font-size: 16px;
    color: #9ba0b0;
    margin-top: 4px;
    font-style: italic;
  }
  .thread {
    width: 100%;
    max-width: 860px;
    flex: 1;
    overflow-y: auto;
    padding: 20px 20px 80px;
    display: flex;
    flex-direction: column;
    gap: 0;
  }
  .post {
    display: flex;
    flex-direction: column;
    padding: 10px 0 10px 0;
    border-bottom: 1px solid var(--border);
    animation: fadeIn 0.4s ease;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .post-meta {
    font-family: var(--font);
    font-size: 11px;
    color: var(--dim);
    margin-bottom: 5px;
    display: flex;
    gap: 10px;
    align-items: baseline;
  }
  .post-name { color: #5f8f5f; }
  .post-num  { color: var(--num); }
  .post-time { color: var(--dim); }
  .post-body {
    font-size: 14px;
    line-height: 1.65;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .post-body .gt { color: var(--green); }
  .typing-indicator {
    padding: 10px 0;
    font-family: var(--font);
    font-size: 11px;
    color: var(--dim);
    letter-spacing: 0.06em;
  }
  .typing-indicator span {
    animation: blink 1.1s step-end infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  .controls {
    position: fixed;
    bottom: 0;
    left: 0; right: 0;
    display: flex;
    justify-content: center;
    gap: 10px;
    padding: 12px 20px;
    background: linear-gradient(transparent, var(--bg) 40%);
  }
  button {
    font-family: var(--font);
    font-size: 11px;
    letter-spacing: 0.06em;
    padding: 5px 14px;
    border-radius: 3px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
  }
  button:hover { border-color: var(--green); color: var(--green); }
  button.active { border-color: var(--green); color: var(--green); }
  .speed-label { font-family: var(--font); font-size: 11px; color: var(--dim); padding: 5px 0; align-self: center; }
</style>
</head>
<body>

<div class="board-header">
  <h1>/fit/ — fitness &amp; health</h1>
  <div class="thread-title">be me thread — continuous</div>
</div>

<div class="thread" id="thread"></div>

<div class="controls">
  <button id="toggleBtn" class="active">pause</button>
  <span class="speed-label">interval</span>
  <button onclick="setSpeed(4000)">slow</button>
  <button onclick="setSpeed(2000)" class="active" id="btn-med">medium</button>
  <button onclick="setSpeed(800)">fast</button>
</div>

<script>
const thread = document.getElementById('thread');
const toggleBtn = document.getElementById('toggleBtn');

let posts = [];
let running = true;
let speed = 2000;
let timer = null;
let typing = null;

function fmtTime() {
  const n = new Date();
  return n.toLocaleDateString('en-US', {month:'2-digit',day:'2-digit',year:'2-digit'})
    + '(' + ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][n.getDay()] + ')'
    + n.toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}

function randId() {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from({length: 8}, () => chars[Math.floor(Math.random() * chars.length)]).join('');
}

function renderPost(text, num) {
  const el = document.createElement('div');
  el.className = 'post';

  const meta = document.createElement('div');
  meta.className = 'post-meta';
  meta.innerHTML = `<span class="post-name">Anonymous</span><span class="post-num">No.${num}</span><span class="post-time">${fmtTime()}</span><span class="post-id">(ID: ${randId()})</span>`;

  const body = document.createElement('div');
  body.className = 'post-body';
  // render greentext lines in green
  body.innerHTML = text.split('\n').map(line =>
    line.startsWith('>') ? `<span class="gt">${escHtml(line)}</span>` : escHtml(line)
  ).join('\n');

  el.appendChild(meta);
  el.appendChild(body);
  return el;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function showTyping() {
  hideTyping();
  typing = document.createElement('div');
  typing.className = 'typing-indicator';
  typing.innerHTML = 'Anonymous <span>is typing…</span>';
  thread.appendChild(typing);
  thread.scrollTop = thread.scrollHeight;
}

function hideTyping() {
  if (typing) { typing.remove(); typing = null; }
}

let fetching = false;

async function fetchNext() {
  if (fetching) return;
  fetching = true;
  showTyping();
  try {
    const res = await fetch('/api/wall/next', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ posts: posts.slice(-6) }),
    });
    const data = await res.json();
    hideTyping();
    if (data.post) {
      const el = renderPost(data.post, data.post_num);
      thread.appendChild(el);
      thread.scrollTop = thread.scrollHeight;
      posts.push(data.post);
      if (posts.length > 60) posts = posts.slice(-60);
    }
  } catch(e) {
    hideTyping();
  }
  fetching = false;
  // wait `speed` ms after the post lands before requesting the next one
  if (running) timer = setTimeout(fetchNext, speed);
}

function setSpeed(ms) {
  speed = ms;
  document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
  if (ms === 4000) document.querySelector('[onclick="setSpeed(4000)"]').classList.add('active');
  if (ms === 2000) document.querySelector('[onclick="setSpeed(2000)"]').classList.add('active');
  if (ms === 800) document.querySelector('[onclick="setSpeed(800)"]').classList.add('active');
}

toggleBtn.addEventListener('click', () => {
  running = !running;
  toggleBtn.textContent = running ? 'pause' : 'resume';
  toggleBtn.classList.toggle('active', running);
  if (running) fetchNext();
  else { clearTimeout(timer); hideTyping(); }
});

// kick off
fetchNext();
</script>
</body>
</html>"""

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FitAnon</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d0f14;
    --surface: #161a23;
    --border: #2a2f3d;
    --text: #c8cdd8;
    --dim: #555d72;
    --green: #4caf70;
    --accent: #5b8ff0;
    --danger: #c05050;
    --font: "SF Mono", "Fira Mono", "Consolas", monospace;
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    font-size: 13px;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 18px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
  }
  header h1 { font-size: 14px; color: var(--green); letter-spacing: 0.04em; }
  header .tag { font-size: 11px; color: var(--dim); }
  .mode-select {
    margin-left: auto;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    font-family: var(--font);
    font-size: 12px;
    padding: 4px 8px;
    border-radius: 4px;
    cursor: pointer;
  }
  .layout {
    display: flex;
    flex: 1;
    min-height: 0;
  }
  /* Chat panel */
  .chat-panel {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    border-right: 1px solid var(--border);
  }
  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .msg { display: flex; flex-direction: column; gap: 3px; max-width: 100%; }
  .msg .label { font-size: 11px; color: var(--dim); }
  .msg.user .label { color: var(--accent); }
  .msg.anon .label { color: var(--green); }
  .msg .bubble {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px 12px;
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.55;
  }
  .msg.user .bubble { border-color: var(--accent); }
  .msg.anon .bubble { border-color: var(--green); }
  .msg .mode-tag {
    font-size: 10px;
    color: var(--dim);
    margin-top: 2px;
  }
  .thinking {
    color: var(--dim);
    font-size: 12px;
    padding: 6px 0;
    animation: blink 1s step-end infinite;
  }
  @keyframes blink { 50% { opacity: 0.3; } }
  .input-row {
    display: flex;
    gap: 8px;
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
  }
  textarea {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    font-family: var(--font);
    font-size: 13px;
    resize: none;
    padding: 8px 10px;
    border-radius: 4px;
    line-height: 1.5;
    min-height: 38px;
    max-height: 120px;
  }
  textarea:focus { outline: none; border-color: var(--accent); }
  button {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 4px;
    padding: 0 16px;
    font-family: var(--font);
    font-size: 12px;
    cursor: pointer;
    flex-shrink: 0;
  }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button:hover:not(:disabled) { filter: brightness(1.15); }
  /* Retrieved panel */
  .retrieved-panel {
    width: 280px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    background: var(--surface);
  }
  .retrieved-panel .panel-title {
    padding: 10px 14px 8px;
    font-size: 11px;
    color: var(--dim);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .retrieved-list {
    flex: 1;
    overflow-y: auto;
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .retrieved-item {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 7px 9px;
    font-size: 11px;
    color: var(--dim);
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.4;
  }
  .empty-state {
    color: var(--dim);
    font-size: 11px;
    padding: 12px;
    text-align: center;
  }
  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>
<header>
  <h1>/fit/anon</h1>
  <span class="tag" id="modelTag">Qwen2.5-3B · loading…</span>
  <select class="mode-select" id="modeSelect">
    {% for m in modes %}
    <option value="{{ m }}"{% if m == 'reply_like_thread' %} selected{% endif %}>{{ m }}</option>
    {% endfor %}
  </select>
</header>
<div class="layout">
  <div class="chat-panel">
    <div class="messages" id="messages">
      <div class="empty-state">start a conversation — it replies as an anonymous /fit/ poster</div>
    </div>
    <div class="input-row">
      <textarea id="input" placeholder="type something..." rows="1"></textarea>
      <button id="sendBtn">send</button>
    </div>
  </div>
  <div class="retrieved-panel">
    <div class="panel-title">retrieved corpus posts</div>
    <div class="retrieved-list" id="retrievedList">
      <div class="empty-state">posts used to ground the last reply appear here</div>
    </div>
  </div>
</div>

<script>
const msgContainer = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const modeSelect = document.getElementById('modeSelect');
const retrievedList = document.getElementById('retrievedList');

let history = [];
let thinking = null;

function addMsg(role, content, modeTag) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const label = document.createElement('div');
  label.className = 'label';
  label.textContent = role === 'user' ? 'you' : 'anon';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = content;
  wrap.appendChild(label);
  wrap.appendChild(bubble);
  if (modeTag) {
    const mt = document.createElement('div');
    mt.className = 'mode-tag';
    mt.textContent = modeTag;
    wrap.appendChild(mt);
  }
  msgContainer.appendChild(wrap);
  msgContainer.scrollTop = msgContainer.scrollHeight;
  return wrap;
}

function showThinking() {
  const el = document.createElement('div');
  el.className = 'thinking';
  el.textContent = '…';
  msgContainer.appendChild(el);
  msgContainer.scrollTop = msgContainer.scrollHeight;
  thinking = el;
}

function hideThinking() {
  if (thinking) { thinking.remove(); thinking = null; }
}

function showRetrieved(posts) {
  retrievedList.innerHTML = '';
  if (!posts || posts.length === 0) {
    retrievedList.innerHTML = '<div class="empty-state">no retrieved posts</div>';
    return;
  }
  posts.forEach((p, i) => {
    const el = document.createElement('div');
    el.className = 'retrieved-item';
    el.textContent = p;
    retrievedList.appendChild(el);
  });
}

async function send() {
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;

  // Remove empty state
  const emptyState = msgContainer.querySelector('.empty-state');
  if (emptyState) emptyState.remove();

  addMsg('user', msg);
  showThinking();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: msg,
        mode: modeSelect.value,
        history: history.slice(-8),  // previous turns only, not the current msg
      }),
    });
    const data = await res.json();
    hideThinking();
    if (data.error) {
      addMsg('anon', `[error: ${data.error}]`);
    } else {
      addMsg('anon', data.reply, `mode: ${data.mode}`);
      // push both turns to history only after successful response
      history.push({ role: 'user', content: msg });
      history.push({ role: 'assistant', content: data.reply });
      showRetrieved(data.retrieved);
    }
  } catch (e) {
    hideThinking();
    addMsg('anon', `[network error: ${e.message}]`);
  }

  sendBtn.disabled = false;
  input.focus();
}

sendBtn.addEventListener('click', send);
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 120) + 'px';
});

fetch('/api/status').then(r => r.json()).then(d => {
  const tag = document.getElementById('modelTag');
  if (d.adapter) {
    tag.textContent = `${d.model} · LoRA ${d.adapter}`;
  } else {
    tag.textContent = `${d.model} · base`;
  }
});
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    global _args
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--adapter", default=DEFAULT_ADAPTER)
    parser.add_argument("--no-adapter", action="store_true", default=True, help="Use base model only (default until adapter trains further)")
    parser.add_argument("--with-adapter", dest="no_adapter", action="store_false", help="Enable LoRA adapter")
    parser.add_argument("--port", type=int, default=8765)
    _args = parser.parse_args()

    print("FitAnon chat server")
    print(f"  model:   {_args.model}")
    if _args.no_adapter:
        print("  adapter: (none — base model)")
    else:
        print(f"  adapter: {_args.adapter}")
    print(f"  port:    {_args.port}")
    print()

    _load_corpus()

    print(f"\nOpen http://localhost:{_args.port}\n")
    app.run(host="0.0.0.0", port=_args.port, debug=False, threaded=False)


if __name__ == "__main__":
    main()
