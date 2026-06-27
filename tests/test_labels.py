"""Pin the three canonical labels (planning.md §3) at the byte level.

These strings are part of the public API contract — any drift breaks
downstream consumers, so each is asserted as a literal here.
"""
from __future__ import annotations

import pytest

from labels import (
    ATTRIBUTION_AI,
    ATTRIBUTION_HUMAN,
    ATTRIBUTION_UNCERTAIN,
    LABEL_AI,
    LABEL_HUMAN,
    LABEL_UNCERTAIN,
    label_for_attribution,
)


# ---- Canonical strings -----------------------------------------------------

def test_label_ai_is_exact_spec_string():
    assert LABEL_AI == "high-confidence AI"


def test_label_human_is_exact_spec_string():
    assert LABEL_HUMAN == "high-confidence human"


def test_label_uncertain_is_exact_spec_string():
    assert LABEL_UNCERTAIN == "uncertain"


# ---- Style guardrails (planning.md §3 "must NOT say") ----------------------

@pytest.mark.parametrize("label", [LABEL_AI, LABEL_HUMAN, LABEL_UNCERTAIN])
def test_no_label_ends_in_punctuation(label):
    assert label[-1] not in ".!?,;:"


def test_label_ai_keeps_acronym_uppercase_and_rest_lowercase():
    # "AI" must be capitalized; "high-confidence" must NOT.
    assert "AI" in LABEL_AI
    assert LABEL_AI.split()[0] == "high-confidence"


def test_label_human_is_fully_lowercase():
    assert LABEL_HUMAN == LABEL_HUMAN.lower()


def test_label_uncertain_is_fully_lowercase():
    assert LABEL_UNCERTAIN == LABEL_UNCERTAIN.lower()


def test_no_label_contains_numeric_confidence():
    # planning.md §3 — labels never embed the score.
    for label in (LABEL_AI, LABEL_HUMAN, LABEL_UNCERTAIN):
        assert not any(ch.isdigit() for ch in label)


# ---- label_for_attribution dispatcher --------------------------------------

def test_ai_attribution_returns_ai_label():
    assert label_for_attribution(ATTRIBUTION_AI) == LABEL_AI


def test_human_attribution_returns_human_label():
    assert label_for_attribution(ATTRIBUTION_HUMAN) == LABEL_HUMAN


def test_uncertain_attribution_returns_uncertain_label():
    assert label_for_attribution(ATTRIBUTION_UNCERTAIN) == LABEL_UNCERTAIN


def test_unknown_attribution_fails_safe_to_uncertain():
    # Never claim high-confidence on an unrecognized verdict.
    assert label_for_attribution("bogus") == LABEL_UNCERTAIN
    assert label_for_attribution("") == LABEL_UNCERTAIN


# ---- Attribution short strings ---------------------------------------------

def test_attribution_strings_pinned():
    assert ATTRIBUTION_AI == "AI"
    assert ATTRIBUTION_HUMAN == "human"
    assert ATTRIBUTION_UNCERTAIN == "uncertain"
