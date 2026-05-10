"""Source-agnostic adapter that turns a raw post payload into a manifest record.

Right now the only supported source is 4chan /fit/, but `build_record` is the
single place that knows the on-disk schema, so 8kun (or any future board) can
plug in by producing the same dict shape.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from . import contamination, normalise, safety

# Coarse semantic tags applied per post. Deliberately conservative — better to
# tag too little than to misclassify.
_TAG_PATTERNS = {
    "discipline_routine": re.compile(r'\b(routine|program|ppl|push pull legs|5x5|stronglifts|gzclp|nsuns)\b', re.I),
    "diet_protocol": re.compile(r'\b(cut|bulk|maingain|kcal|calorie|macros?|protein|carbs?|tdee)\b', re.I),
    "self_loathing": re.compile(r'\b(disgust(?:ed|ing)?|hate myself|hate my body|worthless|pathetic|loser)\b', re.I),
    "transformation": re.compile(r'\b(transformation|before\s*[/&]\s*after|glow[\s-]?up|reborn|new me)\b', re.I),
    "mirror_check": re.compile(r'\b(mirror|reflection|check(?:ed|ing)? myself|how do i look)\b', re.I),
    "physique_compare": re.compile(r'\b(mogged|mog(?:ging|ged)?|height|wrist|frame|aesthetic|brad\s*pit|zyzz)\b', re.I),
    "supplement_question": re.compile(r'\b(creatine|whey|pre[\s-]?workout|ashwagandha|fish oil|vitamin)\b', re.I),
    "pain_injury": re.compile(r'\b(pain|injur(?:ed|y)|tendon|knee|shoulder|back hurts|tweaked)\b', re.I),
    "shame_or_pride": re.compile(r'\b(ashamed|proud|finally|first time|pr|personal best)\b', re.I),
    "cope_or_giveup": re.compile(r'\b(cope|cop(?:ing)?|giving up|give up|why bother|over for me)\b', re.I),
}


def _coarse_tags(text: str) -> list[str]:
    if not text:
        return []
    return [tag for tag, pat in _TAG_PATTERNS.items() if pat.search(text)]


def build_post_uid(source_key: str, board: str, post_no: int) -> str:
    return f"{source_key}:{board}:{post_no}"


def build_record(
    *,
    source_key: str,
    board: str,
    thread_no: int,
    post: dict,
    is_op: bool = False,
) -> Optional[dict]:
    """Convert a raw 4chan post dict into a normalised manifest record.

    Returns None if the post has no usable text.
    """
    post_no = int(post.get("no") or 0)
    if not post_no:
        return None

    com_html = post.get("com") or ""
    plain, quote_removed, reply_targets = normalise.normalise_post(com_html)
    if not plain:
        # Image-only / empty posts: keep the record but with empty text fields.
        plain, quote_removed, reply_targets = "", "", []

    labels = safety.classify(plain)
    model_safe = safety.make_model_safe(quote_removed or plain, labels)
    contaminated = safety.redact_for_contaminated(plain)
    greentext_lines = normalise.extract_greentext_lines(plain)
    contam_scores = contamination.label(plain, labels)
    use_policy = contamination.use_policy_for(labels, contam_scores)

    has_image = bool(post.get("tim")) and bool(post.get("ext"))

    record = {
        "post_uid": build_post_uid(source_key, board, post_no),
        "source_key": source_key,
        "source": "4chan" if source_key.startswith("4chan") else source_key.split("__", 1)[0],
        "board": board,
        "thread_no": thread_no,
        "post_no": post_no,
        "is_op": bool(is_op),
        "subject": post.get("sub") or "",
        "name": post.get("name") or "",
        "trip": post.get("trip") or "",
        "time_unix": int(post.get("time") or 0),
        "scraped_at_unix": int(time.time()),
        "reply_targets": reply_targets,
        "raw_html": com_html,
        "plain_text": plain,
        "quote_removed_text": quote_removed,
        "model_safe_text": model_safe,
        "contaminated_text": contaminated,
        "greentext_lines": greentext_lines,
        "fit_tags": _coarse_tags(quote_removed or plain),
        "safety": labels,
        "contamination": contam_scores,
        "use_policy": use_policy,
        "image": (
            {
                "tim": int(post["tim"]),
                "ext": post["ext"],
                "filename": post.get("filename") or "",
                "w": int(post.get("w") or 0),
                "h": int(post.get("h") or 0),
                "fsize": int(post.get("fsize") or 0),
                "md5": post.get("md5") or "",
            }
            if has_image
            else None
        ),
    }
    return record
