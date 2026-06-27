"""End-to-end /submit + /log tests using the 4 canonical sample texts.

The LLM judge is mocked (`mock_judge` fixture) so tests are hermetic and don't
hit Groq. Stylometric Signal 1 runs for real, so the assertions reflect the
*actual* pipeline behavior, including known M3 calibration gaps.
"""
from __future__ import annotations

import pytest

from tests.samples import AI_PARAGRAPH, EDITED_AI, FORMAL_HUMAN, HUMAN_PARAGRAPH


def _submit(client, text: str, author: str = "tester"):
    resp = client.post("/submit", json={"text": text, "author_id": author})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# ---- Strong branches end-to-end --------------------------------------------

def test_human_paragraph_with_human_judge_hits_strong_human(app_client, mock_judge):
    """Ramen review (stylo ~0.17) + judge Human/0.9 (llm_ai 0.1) → strong-human."""
    mock_judge(label="Human", confidence=0.9)
    out = _submit(app_client, HUMAN_PARAGRAPH)
    assert out["label"] == "high-confidence human"
    assert out["attribution"] == "human"
    assert out["confidence"] < 0.3


def test_formal_human_with_human_judge_hits_strong_human(app_client, mock_judge):
    """Formal-academic sample also lands stylo <0.3 in practice (no em-dashes /
    transitions in this passage). With a strong-human judge, agreement fires.
    """
    mock_judge(label="Human", confidence=0.9)
    out = _submit(app_client, FORMAL_HUMAN)
    assert out["label"] == "high-confidence human"


# ---- Relaxed gate: combined > 0.7 + both lean same way -------------------

def test_ai_paragraph_with_ai_judge_hits_strong_ai(app_client, mock_judge):
    """AI paragraph stylo ~0.68 + judge AI/0.92 (llm_ai 0.92) → combined ~0.80
    > 0.7, both > 0.5 → strong-AI. Verifies the relaxed gate fires for the
    motivating case where one signal is just shy of the strict 0.7 bar but
    both clearly lean AI.
    """
    mock_judge(label="AI", confidence=0.92)
    out = _submit(app_client, AI_PARAGRAPH)
    assert out["label"] == "high-confidence AI"
    assert out["attribution"] == "AI"


# ---- Disagreement / mid-band -----------------------------------------------

def test_human_paragraph_with_ai_judge_lands_uncertain(app_client, mock_judge):
    """Signals disagree (stylo low, judge high-AI) → uncertain."""
    mock_judge(label="AI", confidence=0.9)
    out = _submit(app_client, HUMAN_PARAGRAPH)
    assert out["label"] == "uncertain"


def test_edited_ai_with_mid_judge_lands_uncertain(app_client, mock_judge):
    """Lightly edited AI — judge mid-range (0.55) and stylo ~0.56 → uncertain."""
    mock_judge(label="AI", confidence=0.55)
    out = _submit(app_client, EDITED_AI)
    assert out["label"] == "uncertain"


# ---- LLM-failure fallback path ---------------------------------------------

def test_submit_falls_back_to_uncertain_when_judge_returns_none(app_client, mock_judge_failure):
    out = _submit(app_client, AI_PARAGRAPH)
    assert out["label"] == "uncertain"
    assert out["attribution"] == "uncertain"


# ---- Persistence: /submit writes a full row that /log can read -------------

def test_submission_persists_full_decision_trace(app_client, mock_judge):
    mock_judge(label="AI", confidence=0.92)
    sub = _submit(app_client, AI_PARAGRAPH, author="alice")

    log = app_client.get("/log").get_json()
    entries = log["entries"]
    assert len(entries) == 1

    e = entries[0]
    assert e["content_id"] == sub["submission_id"]
    assert e["creator_id"] == "alice"
    assert e["label"] == sub["label"]
    assert e["confidence"] == sub["confidence"]
    assert e["attribution"] == sub["attribution"]
    assert e["llm_score"] == 0.92
    assert e["stylo_score"] is not None
    assert e["timestamp"]  # ISO timestamp populated


def test_log_orders_newest_first_across_authors(app_client, mock_judge):
    mock_judge(label="AI", confidence=0.92)
    s1 = _submit(app_client, AI_PARAGRAPH, author="alice")
    s2 = _submit(app_client, AI_PARAGRAPH, author="bob")
    s3 = _submit(app_client, AI_PARAGRAPH, author="alice")

    ids = [e["content_id"] for e in app_client.get("/log").get_json()["entries"]]
    assert ids == [s3["submission_id"], s2["submission_id"], s1["submission_id"]]


def test_log_scopes_to_author(app_client, mock_judge):
    mock_judge(label="AI", confidence=0.92)
    _submit(app_client, AI_PARAGRAPH, author="alice")
    _submit(app_client, AI_PARAGRAPH, author="bob")
    _submit(app_client, AI_PARAGRAPH, author="alice")

    alice = app_client.get("/log?author_id=alice").get_json()["entries"]
    bob   = app_client.get("/log?author_id=bob").get_json()["entries"]
    assert len(alice) == 2
    assert len(bob) == 1
    assert all(e["creator_id"] == "alice" for e in alice)


def test_log_unknown_author_returns_empty(app_client):
    entries = app_client.get("/log?author_id=nobody").get_json()["entries"]
    assert entries == []


def test_log_bad_limit_returns_400(app_client):
    assert app_client.get("/log?limit=abc").status_code == 400


# ---- /submit input validation ----------------------------------------------

def test_submit_missing_text_returns_400(app_client):
    resp = app_client.post("/submit", json={"author_id": "x"})
    assert resp.status_code == 400


def test_submit_missing_author_returns_400(app_client):
    resp = app_client.post("/submit", json={"text": "hello"})
    assert resp.status_code == 400


def test_submit_empty_text_returns_400(app_client):
    resp = app_client.post("/submit", json={"text": "   ", "author_id": "x"})
    assert resp.status_code == 400


def test_submit_non_object_body_returns_400(app_client):
    resp = app_client.post("/submit", json=["array body"])
    assert resp.status_code == 400
