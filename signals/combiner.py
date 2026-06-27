"""Combine Signal 1 (stylo_score) and Signal 2 (llm_ai_score) into a final
decision (combined_score + signals_agreed + final_label + attribution),
following planning.md §1's gating rules.

Rules:
    combined_score = (stylo_score + llm_ai_score) / 2

    strong_ai    = combined_score > 0.7  AND  stylo_score > 0.5  AND  llm_ai_score > 0.5
                 → "high-confidence AI"
    strong_human = combined_score < 0.3  AND  stylo_score < 0.5  AND  llm_ai_score < 0.5
                 → "high-confidence human"
    else                                                                     → "uncertain"

The combined-score gate carries the magnitude check; the per-signal `>0.5 /
<0.5` directional check prevents one near-zero signal from being overpowered
by the other (i.e. both signals must at least *lean* the same way).

Tie-breakers:
  * Strict `>` and `<` — exactly 0.7, 0.5, or 0.3 do NOT pass the bar.
  * If Signal 2 fails (llm_ai_score is None): fall back to stylo_score alone,
    force final_label = "uncertain", signals_agreed = False.
"""
from __future__ import annotations

from typing import Optional

_AI_THRESHOLD = 0.7
_HUMAN_THRESHOLD = 0.3
_DIRECTION_MIDPOINT = 0.5

_LABEL_AI = "high-confidence AI"
_LABEL_HUMAN = "high-confidence human"
_LABEL_UNCERTAIN = "uncertain"

_ATTRIBUTION_AI = "AI"
_ATTRIBUTION_HUMAN = "human"
_ATTRIBUTION_UNCERTAIN = "uncertain"


def _attribution_for(label: str) -> str:
    if label == _LABEL_AI:
        return _ATTRIBUTION_AI
    if label == _LABEL_HUMAN:
        return _ATTRIBUTION_HUMAN
    return _ATTRIBUTION_UNCERTAIN


def combine(stylo_score: float, llm_ai_score: Optional[float]) -> dict:
    """Return {combined_score, signals_agreed, final_label, attribution}."""
    if llm_ai_score is None:
        # Signal 2 failed — fall back to stylo alone, force uncertain.
        return {
            "combined_score": stylo_score,
            "signals_agreed": False,
            "final_label":    _LABEL_UNCERTAIN,
            "attribution":    _ATTRIBUTION_UNCERTAIN,
        }

    combined_score = (stylo_score + llm_ai_score) / 2.0

    both_lean_ai    = stylo_score > _DIRECTION_MIDPOINT and llm_ai_score > _DIRECTION_MIDPOINT
    both_lean_human = stylo_score < _DIRECTION_MIDPOINT and llm_ai_score < _DIRECTION_MIDPOINT

    strong_ai    = combined_score > _AI_THRESHOLD    and both_lean_ai
    strong_human = combined_score < _HUMAN_THRESHOLD and both_lean_human

    if strong_ai:
        final_label = _LABEL_AI
        signals_agreed = True
    elif strong_human:
        final_label = _LABEL_HUMAN
        signals_agreed = True
    else:
        final_label = _LABEL_UNCERTAIN
        signals_agreed = False

    return {
        "combined_score": combined_score,
        "signals_agreed": signals_agreed,
        "final_label":    final_label,
        "attribution":    _attribution_for(final_label),
    }
