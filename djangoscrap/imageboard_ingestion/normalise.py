"""Normalise raw 4chan post HTML into plain text + structured reply links.

4chan post `com` field contains HTML like:
  <a href="#p123456" class="quotelink">&gt;&gt;123456</a>
  <span class="quote">&gt;greentext line</span>
  Body text with <br> line breaks and HTML entities.
"""
from __future__ import annotations

import re
import html as html_lib
from typing import List, Tuple

_QUOTELINK_RE = re.compile(r'<a[^>]*class="quotelink"[^>]*>&gt;&gt;(\d+)</a>', re.IGNORECASE)
_GREENTEXT_RE = re.compile(r'<span[^>]*class="quote"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'[ \t]+')
_MULTI_NL_RE = re.compile(r'\n{3,}')


def extract_reply_targets(com_html: str) -> List[int]:
    """Return list of post numbers this post replies to (>>NNNN quotelinks)."""
    if not com_html:
        return []
    return [int(m) for m in _QUOTELINK_RE.findall(com_html)]


def html_to_plain_text(com_html: str) -> str:
    """Full plain-text rendering of a post's HTML, with > preserved on greentext."""
    if not com_html:
        return ""
    s = com_html
    s = _QUOTELINK_RE.sub(lambda m: f">>{m.group(1)}", s)
    s = _GREENTEXT_RE.sub(lambda m: ">" + m.group(1), s)
    s = _BR_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html_lib.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = _MULTI_NL_RE.sub("\n\n", s)
    return s.strip()


def strip_quotes(plain_text: str) -> str:
    """Remove >>NNNN quote refs and >greentext lines, leaving the post's own voice."""
    if not plain_text:
        return ""
    lines = []
    for line in plain_text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        # drop pure ">>123" lines
        if re.match(r'^>>\d+\s*$', stripped):
            continue
        # strip leading >>NNNN tokens (inline replies) before deciding greentext
        without_refs = re.sub(r'^(?:>>\d+\s*)+', '', stripped)
        # if what remains is a greentext line (single > not followed by digit), drop it
        if re.match(r'^>(?!>)', without_refs):
            continue
        # also strip any remaining inline >>NNNN tokens
        cleaned = re.sub(r'>>\d+', '', without_refs).strip()
        if cleaned:
            lines.append(cleaned)
    out = "\n".join(lines)
    out = _MULTI_NL_RE.sub("\n\n", out)
    return out.strip()


def normalise_post(com_html: str) -> Tuple[str, str, List[int]]:
    """Return (plain_text, quote_removed_text, reply_targets)."""
    plain = html_to_plain_text(com_html)
    quote_removed = strip_quotes(plain)
    targets = extract_reply_targets(com_html)
    return plain, quote_removed, targets


def extract_greentext_lines(plain_text: str) -> list[str]:
    """Pull every greentext line (starts with '>' but not '>>') in order.

    Greentext is the structural backbone of /fit/ voice. We keep these as a
    separate list so the dataset builder and grounding layer can lean on them.
    """
    if not plain_text:
        return []
    out: list[str] = []
    for line in plain_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r'^>(?!>)', s):
            # strip the leading '>' and any leading whitespace after it
            cleaned = s.lstrip(">").lstrip()
            if cleaned:
                out.append(cleaned)
    return out
