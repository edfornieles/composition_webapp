#!/usr/bin/env python3
"""Chan-styled monitoring server for the SFT build.

    python scripts/monitor_sft_build.py
    # open http://localhost:9731
"""
import http.server
import json
import random
import re
import subprocess
import time
from pathlib import Path

LOG = Path("/Volumes/Oom/imageboard_corpora/chan_sft/build.log")
OUT = Path("/Volumes/Oom/imageboard_corpora/chan_sft/chan_sft.jsonl")
REDDIT_LOG = Path("/Volumes/Oom/imageboard_corpora/reddit_women40s/scrape.log")
REDDIT_TEXT = Path("/Volumes/Oom/imageboard_corpora/reddit_women40s/text")
PORT = 9731

BOARD_COLORS = {
    "fit": "#d4e8d4", "mu": "#d4d4e8", "lit": "#e8e4d4",
    "tg": "#e8d4d4", "sci": "#d4e8e8", "his": "#e8e0d0",
    "ck":  "#f0e8d0", "out": "#d8e8d0", "g":  "#d4dce8",
    "co":  "#e8d8e4", "a":  "#e8d4e0", "tv": "#e4d4e8",
    "vg":  "#dce4e8", "ic": "#e8e4d8", "p":  "#e0e8e0",
}


def _row_count() -> int:
    if not OUT.exists():
        return 0
    try:
        r = subprocess.run(["wc", "-l", str(OUT)], capture_output=True, text=True)
        return int(r.stdout.strip().split()[0])
    except Exception:
        return 0


def _file_size() -> str:
    if not OUT.exists():
        return "0 B"
    b = OUT.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _log_tail(n: int = 35) -> list[str]:
    if not LOG.exists():
        return ["(log not found)"]
    try:
        r = subprocess.run(["tail", f"-{n}", str(LOG)], capture_output=True, text=True)
        return r.stdout.splitlines()
    except Exception:
        return []


def _parse_progress(lines: list[str]) -> dict:
    info = {"written": 0, "threads": 0, "skipped_seen": 0,
            "skipped_quality": 0, "elapsed": 0, "current_board": ""}
    for line in reversed(lines):
        if "written=" in line:
            try:
                for part in line.split():
                    k, _, v = part.partition("=")
                    v = v.replace(",", "").rstrip("s")
                    if k == "written":      info["written"] = int(v)
                    elif k == "threads":    info["threads"] = int(v)
                    elif k == "skipped_seen":    info["skipped_seen"] = int(v)
                    elif k == "skipped_quality": info["skipped_quality"] = int(v)
                    elif k == "elapsed":    info["elapsed"] = int(v)
            except Exception:
                pass
            break
    for line in reversed(lines):
        m = re.search(r"/([^/\s._]+)/", line)
        if m:
            info["current_board"] = f"/{m.group(1)}/"
            break
    return info


def _sample_posts(n: int = 5) -> list[dict]:
    """Pick n random rows from the output file."""
    if not OUT.exists():
        return []
    try:
        size = OUT.stat().st_size
        if size == 0:
            return []
        rows = []
        seen_offsets = set()
        attempts = 0
        with OUT.open("rb") as f:
            while len(rows) < n and attempts < n * 8:
                attempts += 1
                offset = random.randint(0, max(0, size - 200))
                if offset in seen_offsets:
                    continue
                seen_offsets.add(offset)
                f.seek(offset)
                f.readline()  # skip partial line
                line = f.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    rows.append(row)
                except Exception:
                    continue
        return rows
    except Exception:
        return []


def _greentext(text: str) -> str:
    """Convert >quote lines to green, escape HTML, preserve newlines."""
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    lines = []
    for line in text.split("\n"):
        if line.startswith("&gt;"):
            lines.append(f'<span class="gt">{line}</span>')
        else:
            lines.append(line)
    return "<br>".join(lines)


def _render_post(post_text: str, board: str, post_no: int, is_target: bool = False) -> str:
    bg = BOARD_COLORS.get(board, "#f0ebe3")
    border = "#d9a" if is_target else "#c8b8a0"
    label = "Anonymous" if not is_target else "Anonymous"
    tag = ' <span class="tag">NEW</span>' if is_target else ""
    ts = time.strftime("%m/%d/%y(%a)%H:%M:%S")
    return f"""<div class="post {'target' if is_target else 'ctx'}" style="border-color:{border}">
  <div class="post-hdr" style="background:{bg}">
    <span class="name">{label}</span>{tag}
    &nbsp;<span class="date">{ts}</span>
    &nbsp;<span class="postno">No.<b>{post_no}</b></span>
  </div>
  <div class="post-body">{_greentext(post_text)}</div>
</div>"""


def _reddit_status() -> dict:
    """Count posts per subreddit from reddit scrape."""
    out = {}
    if not REDDIT_TEXT.exists():
        return out
    for f in sorted(REDDIT_TEXT.glob("*.jsonl")):
        try:
            r = subprocess.run(["wc", "-l", str(f)], capture_output=True, text=True)
            n = int(r.stdout.strip().split()[0])
            if n > 0:
                out[f.stem] = n
        except Exception:
            pass
    return out


def _reddit_log_tail() -> list[str]:
    if not REDDIT_LOG.exists():
        return []
    try:
        r = subprocess.run(["tail", "-15", str(REDDIT_LOG)], capture_output=True, text=True)
        return r.stdout.splitlines()
    except Exception:
        return []


def _render_sample(row: dict, idx: int) -> str:
    msgs = row.get("messages", [])
    meta = row.get("metadata", {})
    board = meta.get("board", "?")
    kind = meta.get("kind", "reply")
    source = meta.get("source", "")

    user_content = msgs[1]["content"] if len(msgs) > 1 else ""
    asst_content = msgs[2]["content"] if len(msgs) > 2 else ""

    base_no = random.randint(80000000, 99999999)

    # Parse context posts from user message
    thread_parts = re.split(r"\[(?:OP|Reply \d+)\]", user_content)
    thread_labels = re.findall(r"\[(?:OP|Reply \d+)\]", user_content)

    posts_html = []
    if kind == "op":
        posts_html.append(f"""<div class="op-prompt">
          <span class="board-tag">/{board}/</span> &mdash; start a new thread
        </div>""")
    else:
        for i, (label, body) in enumerate(zip(thread_labels, thread_parts[1:])):
            body = body.replace("\n\nWrite one reply continuing this thread.", "").strip()
            if not body:
                continue
            posts_html.append(_render_post(body[:400], board, base_no + i))

    posts_html.append(_render_post(asst_content[:500], board, base_no + len(posts_html), is_target=True))

    src_tag = f'<span class="src">{source}/{board}</span>'
    kind_tag = f'<span class="kind-tag kind-{kind}">{kind}</span>'

    return f"""<div class="thread-block">
  <div class="thread-meta">{src_tag} {kind_tag}</div>
  {''.join(posts_html)}
</div>"""


def _render() -> str:
    rows = _row_count()
    size = _file_size()
    lines = _log_tail(35)
    prog = _parse_progress(lines)
    reddit_status = _reddit_status()
    reddit_total = sum(reddit_status.values())
    reddit_log = _reddit_log_tail()
    elapsed = prog["elapsed"]
    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    rate = prog["written"] / max(elapsed, 1)
    target = 2_700_000
    pct = min(prog["written"] / target * 100, 100)
    eta_s = int((target - prog["written"]) / max(rate, 1))
    eta_h, eta_m = eta_s // 3600, (eta_s % 3600) // 60

    log_html = "\n".join(f'<div class="log-line">{l}</div>' for l in lines)

    samples = _sample_posts(5)
    samples_html = "".join(_render_sample(r, i) for i, r in enumerate(samples)) or \
        '<div style="color:#999;padding:20px;">Waiting for data…</div>'

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="8">
<title>/{prog['current_board'] or '?'}/ — SFT Build Monitor</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #FFFFEE;
  font-family: Arial, Helvetica, sans-serif;
  font-size: 13px;
  color: #333;
}}

/* ── Top bar ── */
.topbar {{
  background: #800;
  color: #fff;
  padding: 6px 16px;
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
  border-bottom: 2px solid #600;
}}
.topbar h1 {{ font-size: 15px; font-weight: bold; letter-spacing: 0.03em; }}
.topbar .pulse {{ width: 8px; height: 8px; border-radius: 50%; background: #4f4; animation: pulse 1.5s infinite; }}
@keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}
.topbar .ts {{ color: #fcc; font-size: 11px; margin-left: auto; }}

/* ── Stats bar ── */
.statsbar {{
  background: #F0E0D6;
  border-bottom: 1px solid #d9c4b8;
  padding: 8px 16px;
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  align-items: center;
}}
.stat {{ display: flex; flex-direction: column; }}
.stat-label {{ font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: 0.08em; }}
.stat-value {{ font-size: 18px; font-weight: bold; color: #800; }}
.stat-sub {{ font-size: 11px; color: #999; }}
.bar-wrap {{ flex: 1; min-width: 200px; }}
.bar {{ background: #d9c4b8; border-radius: 3px; height: 14px; overflow: hidden; }}
.bar-fill {{ background: #800; height: 100%; border-radius: 3px; }}
.bar-pct {{ font-size: 10px; color: #888; margin-top: 2px; }}

/* ── Layout ── */
.main {{
  display: grid;
  grid-template-columns: 1fr 380px;
  height: calc(100vh - 84px);
}}

/* ── Samples pane ── */
.samples {{
  overflow-y: auto;
  padding: 12px;
  background: #FFFFEE;
  border-right: 1px solid #d9c4b8;
}}
.samples h2 {{
  font-size: 11px;
  color: #800;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 10px;
  padding-bottom: 4px;
  border-bottom: 1px solid #d9c4b8;
}}

.thread-block {{
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 2px solid #d9c4b8;
}}
.thread-meta {{
  font-size: 10px;
  color: #aaa;
  margin-bottom: 6px;
  display: flex;
  gap: 6px;
  align-items: center;
}}
.board-tag {{ background: #800; color: #fff; padding: 1px 5px; border-radius: 2px; font-weight: bold; }}
.src {{ color: #999; }}
.kind-tag {{
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 2px;
  font-weight: bold;
}}
.kind-op {{ background: #4a8; color: #fff; }}
.kind-reply {{ background: #448; color: #fff; }}

.post {{
  border: 1px solid #c8b8a0;
  border-radius: 3px;
  margin-bottom: 6px;
  background: #fff;
  overflow: hidden;
}}
.post.target {{
  border-color: #800;
  box-shadow: 0 0 0 1px #800;
}}
.post-hdr {{
  padding: 4px 8px;
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}}
.name {{ color: #117743; font-weight: bold; }}
.date {{ color: #aaa; }}
.postno {{ color: #333; }}
.postno b {{ color: #800; }}
.tag {{
  background: #800;
  color: #fff;
  font-size: 9px;
  padding: 1px 4px;
  border-radius: 2px;
  font-weight: bold;
  letter-spacing: 0.05em;
}}
.post-body {{
  padding: 6px 10px 8px;
  line-height: 1.5;
  font-size: 12px;
  word-break: break-word;
  white-space: pre-wrap;
  font-family: Arial, Helvetica, sans-serif;
}}
.gt {{ color: #789922; }}

.op-prompt {{
  font-size: 11px;
  color: #888;
  margin-bottom: 6px;
  font-style: italic;
}}

/* ── Log pane ── */
.logpane {{
  display: flex;
  flex-direction: column;
  background: #1a1a1a;
  overflow: hidden;
}}
.logpane h2 {{
  font-size: 10px;
  color: #666;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 8px 12px;
  border-bottom: 1px solid #222;
  background: #111;
}}
.log-scroll {{
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
}}
.log-line {{
  font-family: 'SF Mono', 'Consolas', monospace;
  font-size: 11px;
  color: #5a9;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-all;
}}
</style>
</head>
<body>

<div class="topbar">
  <div class="pulse"></div>
  <h1>SFT Build Monitor — {prog['current_board'] or 'loading…'}</h1>
  <span class="ts">Refreshes every 8s &nbsp;·&nbsp; {time.strftime('%H:%M:%S')}</span>
</div>

<div class="statsbar">
  <div class="stat">
    <span class="stat-label">Rows written</span>
    <span class="stat-value">{rows:,}</span>
    <span class="stat-sub">{size}</span>
  </div>
  <div class="stat">
    <span class="stat-label">Threads</span>
    <span class="stat-value">{prog['threads']:,}</span>
    <span class="stat-sub">board: {prog['current_board'] or '—'}</span>
  </div>
  <div class="stat">
    <span class="stat-label">Rate</span>
    <span class="stat-value">{rate:.0f}</span>
    <span class="stat-sub">rows/sec</span>
  </div>
  <div class="stat">
    <span class="stat-label">Elapsed</span>
    <span class="stat-value">{h:02d}:{m:02d}:{s:02d}</span>
    <span class="stat-sub">ETA: {eta_h}h {eta_m}m</span>
  </div>
  <div class="stat">
    <span class="stat-label">Skipped</span>
    <span class="stat-value">{prog['skipped_seen']:,}</span>
    <span class="stat-sub">deduped</span>
  </div>
  <div class="bar-wrap">
    <div class="stat-label" style="margin-bottom:4px">Progress ~2.7M target</div>
    <div class="bar"><div class="bar-fill" style="width:{pct:.2f}%"></div></div>
    <div class="bar-pct">{pct:.2f}% &nbsp; {prog['written']:,} / 2,700,000</div>
  </div>
</div>

<div class="main">
  <div class="samples">
    <h2>Live samples from training data</h2>
    {samples_html}
  </div>
  <div class="logpane">
    <h2>Build log</h2>
    <div class="log-scroll" id="log">
      {log_html}
    </div>
  </div>
</div>

<style>
.reddit-panel {{
  background: #fff8f4;
  border-top: 2px solid #d9c4b8;
  padding: 12px 16px;
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  align-items: flex-start;
  font-size: 12px;
}}
.reddit-panel h3 {{
  width: 100%;
  font-size: 11px;
  color: #800;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 4px;
}}
.reddit-subs {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  flex: 1;
}}
.reddit-sub {{
  background: #f0e0d6;
  border: 1px solid #d9c4b8;
  border-radius: 3px;
  padding: 3px 8px;
  font-size: 11px;
}}
.reddit-sub .sub-name {{ color: #800; font-weight: bold; }}
.reddit-sub .sub-count {{ color: #666; }}
.reddit-log-mini {{
  font-family: 'SF Mono', monospace;
  font-size: 10px;
  color: #888;
  background: #1a1a1a;
  color: #5a9;
  padding: 6px 10px;
  border-radius: 4px;
  white-space: pre-wrap;
  max-width: 400px;
  line-height: 1.5;
}}
</style>

<div class="reddit-panel">
  <h3>Reddit scrape — r/women40s corpus &nbsp; <span style="color:#666;font-weight:normal">{reddit_total:,} posts so far</span></h3>
  <div class="reddit-subs">
    {''.join(f'<div class="reddit-sub"><span class="sub-name">r/{s}</span> <span class="sub-count">{n:,}</span></div>' for s, n in sorted(reddit_status.items(), key=lambda x: -x[1])) or '<span style="color:#aaa">scraping…</span>'}
  </div>
  <div class="reddit-log-mini">{'<br>'.join(reddit_log[-8:]) or 'starting…'}</div>
</div>

<script>
  var l = document.getElementById('log');
  if (l) l.scrollTop = l.scrollHeight;
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = _render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Monitor running at http://localhost:{PORT}")
    http.server.HTTPServer(("", PORT), Handler).serve_forever()
