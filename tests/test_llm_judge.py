"""Signal 2 (Groq LLM-as-judge) tests.

The Groq client is mocked — no network calls — so we exercise the parsing,
Pydantic validation, and llm_ai_score conversion in isolation.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from signals.llm_judge import JudgeResponse, judge


def _fake_completion(content: str):
    """Build the nested mock the Groq SDK returns from chat.completions.create."""
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


# ---- Pydantic contract enforcement -----------------------------------------

def test_schema_accepts_valid_ai_response():
    ok = JudgeResponse.model_validate(
        {"label": "AI", "reasoning": "uniform cadence", "confidence": 0.85}
    )
    assert ok.label == "AI"
    assert ok.confidence == 0.85


@pytest.mark.parametrize("bad", [
    {"label": "maybe", "reasoning": "x", "confidence": 0.5},   # bad enum
    {"label": "AI",    "reasoning": "",  "confidence": 0.5},   # empty reasoning
    {"label": "AI",    "reasoning": "x", "confidence": 1.5},   # > 1
    {"label": "AI",    "reasoning": "x", "confidence": -0.1},  # < 0
    {"label": "AI",    "reasoning": "x"},                       # missing confidence
    {"reasoning": "x", "confidence": 0.5},                      # missing label
])
def test_schema_rejects_malformed_response(bad):
    with pytest.raises(ValidationError):
        JudgeResponse.model_validate(bad)


# ---- Happy path: conversion to llm_ai_score --------------------------------

@patch("signals.llm_judge.Groq")
def test_judge_ai_label_passes_confidence_through(MockGroq, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    MockGroq.return_value.chat.completions.create.return_value = _fake_completion(
        '{"label":"AI","reasoning":"too uniform","confidence":0.85}'
    )
    out = judge("some text")
    assert out["llm_label"] == "AI"
    assert out["llm_confidence"] == 0.85
    assert out["llm_ai_score"] == 0.85
    assert out["llm_rationale"] == "too uniform"


@patch("signals.llm_judge.Groq")
def test_judge_human_label_inverts_to_low_ai_score(MockGroq, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    MockGroq.return_value.chat.completions.create.return_value = _fake_completion(
        '{"label":"Human","reasoning":"bursty voice","confidence":0.9}'
    )
    out = judge("...")
    assert out["llm_label"] == "Human"
    assert out["llm_ai_score"] == pytest.approx(0.1)


@patch("signals.llm_judge.Groq")
def test_judge_boundary_confidence_passes_through(MockGroq, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    MockGroq.return_value.chat.completions.create.return_value = _fake_completion(
        '{"label":"AI","reasoning":"x","confidence":1.0}'
    )
    out = judge("...")
    assert out["llm_ai_score"] == 1.0


# ---- Failure modes all return None -----------------------------------------

def test_judge_returns_none_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert judge("...") is None


@patch("signals.llm_judge.Groq")
def test_judge_returns_none_on_malformed_json(MockGroq, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    MockGroq.return_value.chat.completions.create.return_value = _fake_completion(
        "{not json"
    )
    assert judge("...") is None


@patch("signals.llm_judge.Groq")
def test_judge_returns_none_on_invalid_label(MockGroq, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    MockGroq.return_value.chat.completions.create.return_value = _fake_completion(
        '{"label":"Maybe","reasoning":"x","confidence":0.5}'
    )
    assert judge("...") is None


@patch("signals.llm_judge.Groq")
def test_judge_returns_none_on_out_of_range_confidence(MockGroq, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    MockGroq.return_value.chat.completions.create.return_value = _fake_completion(
        '{"label":"AI","reasoning":"x","confidence":1.5}'
    )
    assert judge("...") is None


@patch("signals.llm_judge.Groq")
def test_judge_returns_none_on_empty_reasoning(MockGroq, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    MockGroq.return_value.chat.completions.create.return_value = _fake_completion(
        '{"label":"AI","reasoning":"","confidence":0.8}'
    )
    assert judge("...") is None


@patch("signals.llm_judge.Groq")
def test_judge_returns_none_on_network_error(MockGroq, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    MockGroq.return_value.chat.completions.create.side_effect = Exception("connection")
    assert judge("...") is None
