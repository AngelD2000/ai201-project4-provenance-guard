"""Signal 1 (stylometric heuristics) tests.

Covers the router, the normalization helper, and end-to-end stylo scoring on
the four canonical sample texts. Assertions on the 4-sample scores describe
*observed* M3 behavior, not aspirational targets — known calibration gaps
are documented inline.
"""
from __future__ import annotations

import pytest

from signals.stylometric import _normalize, compute_stylo_score, route
from tests.samples import AI_PARAGRAPH, EDITED_AI, FORMAL_HUMAN, HUMAN_PARAGRAPH


# ---- Router -----------------------------------------------------------------

def test_router_picks_short_form_for_tweet():
    assert route("ok so the ramen was fine i guess") == "short_form"


def test_router_picks_essay_for_long_single_line():
    # > 500 words on one line → falls through to essay.
    text = "This is a sentence. " * 200
    assert route(text) == "essay"


def test_router_picks_poetry_for_short_lines():
    text = "\n".join(["the quiet rain falls"] * 4)
    assert route(text) == "poetry"


def test_router_picks_essay_for_long_paragraph():
    assert route(AI_PARAGRAPH) == "essay"


# ---- Normalization ----------------------------------------------------------

def test_normalize_clips_below_human_min_to_zero():
    assert _normalize(-1.0, 0.0, 10.0, inverted=False) == 0.0


def test_normalize_clips_above_ai_max_to_one():
    assert _normalize(20.0, 0.0, 10.0, inverted=False) == 1.0


def test_normalize_linear_midpoint():
    assert _normalize(5.0, 0.0, 10.0, inverted=False) == 0.5


def test_normalize_inverted_is_mirror_of_direct():
    direct = _normalize(7.0, 0.0, 10.0, inverted=False)
    inverted = _normalize(7.0, 0.0, 10.0, inverted=True)
    assert inverted == pytest.approx(1.0 - direct)


def test_normalize_degenerate_bounds_returns_zero():
    # human_min == ai_max would divide by zero — guarded by the function.
    assert _normalize(5.0, 5.0, 5.0, inverted=False) == 0.0


# ---- compute_stylo_score shape ---------------------------------------------

def test_compute_stylo_score_returns_required_keys():
    out = compute_stylo_score("Hello world this is a test sentence.")
    assert set(out.keys()) == {"engine", "features", "stylo_score"}


def test_stylo_score_is_in_unit_interval():
    out = compute_stylo_score(AI_PARAGRAPH)
    assert 0.0 <= out["stylo_score"] <= 1.0


def test_every_feature_has_raw_and_normalized_in_unit_interval():
    out = compute_stylo_score(AI_PARAGRAPH)
    for feature in out["features"].values():
        assert "raw" in feature
        assert "normalized" in feature
        assert 0.0 <= feature["normalized"] <= 1.0


def test_compute_stylo_is_deterministic():
    text = "Furthermore — moreover. Additionally, consequently."
    assert compute_stylo_score(text) == compute_stylo_score(text)


# ---- Sample-text stylo scores ----------------------------------------------

def test_clearly_human_paragraph_scores_low():
    """Ramen review — bursty, conversational, no em-dashes or transitions."""
    out = compute_stylo_score(HUMAN_PARAGRAPH)
    assert out["engine"] == "essay"
    assert out["stylo_score"] < 0.3


def test_clearly_ai_paragraph_scores_above_midpoint():
    """AI paragraph — transition_density saturates but no em-dashes.

    Documents a known calibration gap: stylo lands ~0.53 here, NOT >0.7.
    The em_dash_density feature dominates the AI direction in the essay
    engine; AI text without literal em-dash characters can't reach the
    strong-AI threshold on stylometric features alone. The judge has to
    carry the call. See planning.md §1 calibration note.
    """
    out = compute_stylo_score(AI_PARAGRAPH)
    assert out["engine"] == "essay"
    assert out["stylo_score"] > 0.4


def test_formal_human_essay_scores_in_observed_band():
    """Formal academic prose. Spec says 'may score mid-high', but the
    specific sample has no em-dashes / transitions so stylo lands ~0.15.
    This is a *false negative on the description*, not the system — the
    test pins observed behavior so calibration changes show up as diffs.
    """
    out = compute_stylo_score(FORMAL_HUMAN)
    assert out["engine"] == "essay"
    assert 0.0 <= out["stylo_score"] < 0.4


def test_edited_ai_essay_scores_mid_range():
    """Lightly edited AI — has a real em-dash, no transitions, mixed cadence."""
    out = compute_stylo_score(EDITED_AI)
    assert out["engine"] == "essay"
    assert 0.3 < out["stylo_score"] < 0.8
