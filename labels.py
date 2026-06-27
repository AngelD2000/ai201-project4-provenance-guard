"""Canonical transparency-label strings (planning.md §3).

These three strings ARE the public API contract. Exact case, no trailing
punctuation, no variation. Consuming platforms render their own user-facing
prose; the system's contract is just the tag.

Single source of truth — combiner.py + app.py import from here so no string
literal for a label exists anywhere else in the codebase.
"""
from __future__ import annotations

# Public label strings — exactly as written in planning.md §3.
LABEL_AI = "high-confidence AI"
LABEL_HUMAN = "high-confidence human"
LABEL_UNCERTAIN = "uncertain"

# Short-form attribution values (the `attribution` field on /submit).
ATTRIBUTION_AI = "AI"
ATTRIBUTION_HUMAN = "human"
ATTRIBUTION_UNCERTAIN = "uncertain"


def label_for_attribution(attribution: str) -> str:
    """Map the short attribution ('AI' | 'human' | 'uncertain') to the public label.

    Unknown attribution defaults to "uncertain" — fail safe, never claim
    high-confidence on an unrecognized verdict.
    """
    if attribution == ATTRIBUTION_AI:
        return LABEL_AI
    if attribution == ATTRIBUTION_HUMAN:
        return LABEL_HUMAN
    return LABEL_UNCERTAIN
