"""Central safety-decision object for the local large-corpus pipeline.

Distinct from the existing text-safety regex (`safety.py`) which classifies
post *text* for the persona pipeline. This module makes a single binary
decision per *image+post pair*:

    allow                 — image may be previewed, embedded, captioned,
                            shown in FiftyOne, exported to ImagePlot, and
                            symlinked into materialised folders.
    quarantine_no_preview — bytes saved to quarantine/no_preview/ ONLY.
                            Never thumbnailed, embedded, captioned, or shown.
    delete_immediately    — caller should drop bytes; nothing on disk.

Reason codes (subset attached on every non-allow decision):
    suspected_underage_sexual_content   (replaces casual "CP")
    illegal_sexual_content
    doxxing
    hate_symbols_or_extremism
    severe_violence_or_gore
    eating_disorder_self_harm
    unknown_corrupt

Confidence is 0.0–1.0. We err very strongly on the side of quarantine when in
doubt — visual classifiers here are conservative regex / keyword based; the
goal is not to be a moderation system but to keep dangerous content out of
preview surfaces.
"""
from __future__ import annotations

import dataclasses as _dc
from typing import List, Optional

ALLOW = "allow"
QUARANTINE = "quarantine_no_preview"
DELETE = "delete_immediately"

REASON_SUSPECTED_UNDERAGE = "suspected_underage_sexual_content"
REASON_ILLEGAL_SEXUAL = "illegal_sexual_content"
REASON_DOXXING = "doxxing"
REASON_HATE = "hate_symbols_or_extremism"
REASON_VIOLENCE = "severe_violence_or_gore"
REASON_ED_SELF_HARM = "eating_disorder_self_harm"
REASON_CORRUPT = "unknown_corrupt"

# Fatal reasons → delete bytes immediately, do not even quarantine.
_DELETE_REASONS = frozenset({REASON_SUSPECTED_UNDERAGE, REASON_ILLEGAL_SEXUAL})


@_dc.dataclass
class SafetyDecision:
    decision: str                # ALLOW | QUARANTINE | DELETE
    reason_codes: List[str]
    confidence: float            # 0.0 (no opinion) → 1.0 (certain)
    notes: str = ""

    @property
    def allow(self) -> bool:
        return self.decision == ALLOW

    @property
    def quarantine_no_preview(self) -> bool:
        return self.decision == QUARANTINE

    @property
    def delete_immediately(self) -> bool:
        return self.decision == DELETE

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
            "confidence": float(self.confidence),
            "notes": self.notes,
        }


# ── Decision logic ──────────────────────────────────────────────────────────

def decide_for_post(post_record: dict, *, text_safety: Optional[dict] = None) -> SafetyDecision:
    """Build a SafetyDecision from an existing manifest post record.

    `text_safety` is the dict produced by safety.py.classify_post_text; if
    omitted we read post_record['safety'] which is normally present.
    """
    text_safety = text_safety or post_record.get("safety") or {}
    reasons: list[str] = []
    conf = 0.0

    if text_safety.get("likely_cp_indicator"):
        # Renamed from the casual "CP" shorthand in upstream regex output.
        reasons.append(REASON_SUSPECTED_UNDERAGE)
        conf = max(conf, 0.95)
    if text_safety.get("likely_doxxing"):
        reasons.append(REASON_DOXXING)
        conf = max(conf, 0.6)
    if text_safety.get("likely_hate"):
        reasons.append(REASON_HATE)
        conf = max(conf, 0.5)
    if text_safety.get("likely_violence_incite"):
        reasons.append(REASON_VIOLENCE)
        conf = max(conf, 0.5)
    if text_safety.get("likely_eating_disorder"):
        reasons.append(REASON_ED_SELF_HARM)
        conf = max(conf, 0.4)

    if any(r in _DELETE_REASONS for r in reasons):
        return SafetyDecision(DELETE, reasons, conf, notes="Hard-stop reason; bytes not retained.")
    if reasons:
        return SafetyDecision(QUARANTINE, reasons, conf,
                              notes="Bytes retained in quarantine/no_preview/ ONLY.")
    return SafetyDecision(ALLOW, [], 0.0)


def decide_for_unreadable_image(reason: str = "Pillow could not verify") -> SafetyDecision:
    return SafetyDecision(QUARANTINE, [REASON_CORRUPT], 1.0, notes=reason)


def merge(*decisions: SafetyDecision) -> SafetyDecision:
    """Worst-case merge: DELETE > QUARANTINE > ALLOW."""
    if not decisions:
        return SafetyDecision(ALLOW, [], 0.0)
    if any(d.delete_immediately for d in decisions):
        codes, conf = [], 0.0
        for d in decisions:
            if d.delete_immediately:
                codes.extend(d.reason_codes)
                conf = max(conf, d.confidence)
        return SafetyDecision(DELETE, sorted(set(codes)), conf)
    if any(d.quarantine_no_preview for d in decisions):
        codes, conf = [], 0.0
        for d in decisions:
            if not d.allow:
                codes.extend(d.reason_codes)
                conf = max(conf, d.confidence)
        return SafetyDecision(QUARANTINE, sorted(set(codes)), conf)
    return SafetyDecision(ALLOW, [], 0.0)
