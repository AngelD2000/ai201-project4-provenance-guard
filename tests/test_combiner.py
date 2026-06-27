"""Tests for the agreement-gating combiner. Pure function — no fixtures."""
from __future__ import annotations

import pytest

from signals.combiner import combine


# ---- Strong-AI branch (combined > 0.7 AND both lean AI) --------------------

def test_strong_ai_both_well_above_threshold():
    out = combine(0.85, 0.80)
    assert out["final_label"] == "high-confidence AI"
    assert out["attribution"] == "AI"
    assert out["signals_agreed"] is True
    assert out["combined_score"] == pytest.approx(0.825)


def test_strong_ai_one_signal_below_0_7_but_combined_above():
    # The motivating case for the relaxed gate: stylo 0.677, llm 0.80.
    # combined=0.739 > 0.7, both > 0.5 → strong-AI.
    out = combine(0.677, 0.80)
    assert out["final_label"] == "high-confidence AI"
    assert out["signals_agreed"] is True


def test_strong_ai_just_above_combined_threshold():
    # combined = (0.51 + 0.91) / 2 = 0.71 > 0.7; both > 0.5 → strong-AI.
    out = combine(0.51, 0.91)
    assert out["final_label"] == "high-confidence AI"


# ---- Strong-Human branch (combined < 0.3 AND both lean human) --------------

def test_strong_human_both_well_below_threshold():
    out = combine(0.10, 0.05)
    assert out["final_label"] == "high-confidence human"
    assert out["attribution"] == "human"
    assert out["signals_agreed"] is True
    assert out["combined_score"] == pytest.approx(0.075)


def test_strong_human_one_signal_above_0_3_but_combined_below():
    # Symmetric to the AI motivating case: stylo 0.32, llm 0.05.
    # combined=0.185 < 0.3, both < 0.5 → strong-human.
    out = combine(0.32, 0.05)
    assert out["final_label"] == "high-confidence human"
    assert out["signals_agreed"] is True


# ---- Strict-boundary tie-breakers (combined threshold) ----------------------

def test_exactly_0_7_combined_does_not_count_as_strong_ai():
    # combined = exactly 0.7 → strict > fails → uncertain.
    out = combine(0.7, 0.7)
    assert out["final_label"] == "uncertain"
    assert out["signals_agreed"] is False


def test_exactly_0_3_combined_does_not_count_as_strong_human():
    out = combine(0.3, 0.3)
    assert out["final_label"] == "uncertain"
    assert out["signals_agreed"] is False


# ---- Directional check (the "both lean same way" guard) --------------------

def test_high_combined_but_one_signal_at_midpoint_not_strong():
    # combined = (0.5 + 0.95) / 2 = 0.725 > 0.7, BUT stylo == 0.5 (not > 0.5)
    # fails the directional check → uncertain. Prevents one-signal carrying.
    out = combine(0.5, 0.95)
    assert out["final_label"] == "uncertain"


def test_low_combined_but_one_signal_at_midpoint_not_strong_human():
    # combined = (0.5 + 0.05) / 2 = 0.275 < 0.3, but stylo == 0.5 fails strict.
    out = combine(0.5, 0.05)
    assert out["final_label"] == "uncertain"


def test_high_combined_but_one_signal_below_midpoint_not_strong():
    # combined = (0.45 + 0.97) / 2 = 0.71 > 0.7, but stylo < 0.5 says "leans
    # human" — directional check fails → uncertain.
    out = combine(0.45, 0.97)
    assert out["final_label"] == "uncertain"


# ---- Mid-band / disagreement -----------------------------------------------

def test_midpoint_yields_uncertain():
    out = combine(0.5, 0.5)
    assert out["final_label"] == "uncertain"
    assert out["signals_agreed"] is False


def test_signals_disagree_yields_uncertain_even_if_avg_lands_near_threshold():
    # 0.95 + 0.05 averages to 0.5 — well below either gate.
    out = combine(0.95, 0.05)
    assert out["final_label"] == "uncertain"
    assert out["combined_score"] == pytest.approx(0.5)


# ---- LLM-failure fallback (§1 tie-breaker) ---------------------------------

def test_llm_failure_forces_uncertain_even_when_stylo_strong():
    out = combine(0.92, None)
    assert out["final_label"] == "uncertain"
    assert out["attribution"] == "uncertain"
    assert out["signals_agreed"] is False
    # combined_score falls back to stylo alone, per spec.
    assert out["combined_score"] == 0.92


def test_llm_failure_uncertain_even_when_stylo_low():
    out = combine(0.05, None)
    assert out["final_label"] == "uncertain"
    assert out["signals_agreed"] is False


# ---- Combined-score formula -------------------------------------------------

def test_combined_score_is_plain_average():
    out = combine(0.4, 0.6)
    assert out["combined_score"] == pytest.approx(0.5)
