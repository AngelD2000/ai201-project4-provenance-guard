"""Signal 1 — Stylometric Heuristics.

Routes raw text to one of three engines (essay / poetry / short_form), scores
three monotonic features per engine, normalizes them to [0, 1] in the
AI-direction via per-feature (human_min, ai_max) bounds, and returns a
weighted-mean `stylo_score` along with the raw + normalized feature dict for
audit logging.

Bounds and weights are placeholders — tune from sample text.
"""
from __future__ import annotations

import re
import statistics

EM_DASH = "—"

_WORD_RE = re.compile(r"\b\w+\b")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+\s+")


# ---- Router -----------------------------------------------------------------

def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def _non_empty_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def route(text: str) -> str:
    words = _word_count(text)
    lines = _non_empty_lines(text)
    mean_line_len = statistics.mean(len(ln) for ln in lines) if lines else 0
    if words <= 500 and len(lines) <= 2:
        return "short_form"
    if len(lines) >= 3 and mean_line_len < 60:
        return "poetry"
    return "essay"


# ---- Normalization ----------------------------------------------------------

def _normalize(value: float, human_min: float, ai_max: float, inverted: bool) -> float:
    """Min-max → [0, 1] in the AI-direction.

    Direct (high = AI):   clip((x - human_min) / (ai_max - human_min), 0, 1)
    Inverted (low = AI):  1 - direct
    """
    if ai_max == human_min:
        return 0.0
    score = (value - human_min) / (ai_max - human_min)
    score = max(0.0, min(1.0, score))
    return 1.0 - score if inverted else score


# ---- Essay engine -----------------------------------------------------------

_TRANSITION_WORDS = {
    "however", "moreover", "furthermore", "additionally",
    "consequently", "therefore", "thus", "hence",
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _burstiness(text: str) -> float:
    lens = [len(_WORD_RE.findall(s)) for s in _sentences(text)]
    return statistics.stdev(lens) if len(lens) >= 2 else 0.0


def _em_dash_density(text: str) -> float:
    words = _word_count(text) or 1
    return (text.count(EM_DASH) / words) * 100


def _transition_density(text: str) -> float:
    tokens = [w.lower() for w in _WORD_RE.findall(text)]
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in _TRANSITION_WORDS)
    return (hits / len(tokens)) * 100


# ---- Poetry engine ----------------------------------------------------------

# TODO: wire a real word-frequency list so rarity isn't a unique-ratio proxy.
_AI_CLICHE_PHRASES = {
    "tapestry of",
    "whispers of the wind",
    "dance of light",
    # TODO: expand to the ~30 phrases described in §1.
}


def _mean_word_rarity(text: str) -> float:
    """Placeholder rarity: unique-token ratio. Replace with log-rank lookup."""
    tokens = [w.lower() for w in _WORD_RE.findall(text)]
    if not tokens:
        return 0.0
    return len(set(tokens)) / len(tokens)


def _cliche_phrase_count(text: str) -> float:
    lowered = text.lower()
    return float(sum(1 for phrase in _AI_CLICHE_PHRASES if phrase in lowered))


def _line_length_variance(text: str) -> float:
    lens = [len(ln) for ln in _non_empty_lines(text)]
    return statistics.stdev(lens) if len(lens) >= 2 else 0.0


# ---- Short-form engine ------------------------------------------------------

def _caps_ratio(text: str) -> float:
    words = _WORD_RE.findall(text)
    if not words:
        return 0.0
    nonstd = sum(1 for w in words if w.isupper() and len(w) > 1)
    return nonstd / len(words)


def _fragment_ratio(text: str) -> float:
    sents = _sentences(text)
    if not sents:
        return 0.0
    frags = sum(1 for s in sents if len(_WORD_RE.findall(s)) < 4)
    return frags / len(sents)


def _lowercase_start_ratio(text: str) -> float:
    sents = _sentences(text)
    if not sents:
        return 0.0
    return sum(1 for s in sents if s and s[0].islower()) / len(sents)


# ---- Engine registry --------------------------------------------------------

# (human_min, ai_max, inverted) — TODO: hand-tune from sample text.
_ENGINES: dict[str, dict] = {
    "essay": {
        "features": {
            "burstiness_score":   _burstiness,
            "em_dash_density":    _em_dash_density,
            "transition_density": _transition_density,
        },
        "bounds": {
            "burstiness_score":   (3.0, 12.0, True),
            "em_dash_density":    (0.0, 1.5,  False),
            "transition_density": (0.0, 2.0,  False),
        },
        # em_dash_density is informative when *present* but absence isn't
        # evidence of human — modern human writers use em-dashes too, AI essays
        # often don't. Down-weighted so absence doesn't tank the score.
        "weights": {
            "burstiness_score":   0.425,
            "em_dash_density":    0.15,
            "transition_density": 0.425,
        },
    },
    "poetry": {
        "features": {
            "mean_word_rarity":     _mean_word_rarity,
            "cliche_phrase_count":  _cliche_phrase_count,
            "line_length_variance": _line_length_variance,
        },
        "bounds": {
            "mean_word_rarity":     (0.40, 0.85, True),
            "cliche_phrase_count":  (0.0,  3.0,  False),
            "line_length_variance": (5.0,  25.0, True),
        },
        "weights": {
            "mean_word_rarity":     1 / 3,
            "cliche_phrase_count":  1 / 3,
            "line_length_variance": 1 / 3,
        },
    },
    "short_form": {
        "features": {
            "caps_ratio":            _caps_ratio,
            "fragment_ratio":        _fragment_ratio,
            "lowercase_start_ratio": _lowercase_start_ratio,
        },
        "bounds": {
            "caps_ratio":            (0.0, 0.15, True),
            "fragment_ratio":        (0.0, 0.50, True),
            # Direction flipped from inverted → direct after Kaggle tweets analysis:
            # AI over-applies lowercase voice (mean 0.67 vs human 0.10).
            "lowercase_start_ratio": (0.10, 0.67, False),
        },
        "weights": {
            "caps_ratio":            1 / 3,
            "fragment_ratio":        1 / 3,
            "lowercase_start_ratio": 1 / 3,
        },
    },
}


def compute_stylo_score(text: str) -> dict:
    """Run Signal 1 end-to-end.

    Returns:
        {
            "engine": "essay" | "poetry" | "short_form",
            "features": { feature_name: {"raw": float, "normalized": float}, ... },
            "stylo_score": float ∈ [0, 1],   # higher = more AI-like
        }
    """
    engine = route(text)
    cfg = _ENGINES[engine]

    features: dict[str, dict[str, float]] = {}
    score = 0.0
    for name, extract in cfg["features"].items():
        raw = float(extract(text))
        human_min, ai_max, inverted = cfg["bounds"][name]
        normalized = _normalize(raw, human_min, ai_max, inverted)
        features[name] = {"raw": raw, "normalized": normalized}
        score += cfg["weights"][name] * normalized

    return {
        "engine": engine,
        "features": features,
        "stylo_score": score,
    }
