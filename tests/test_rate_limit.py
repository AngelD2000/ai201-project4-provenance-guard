"""Rate-limit tests for POST /submit.

Configured limit: 10/min + 100/day per IP. See README for the writer-vs-abuser
math. Tests use a dedicated client fixture that ARMs the limiter (the default
app_client exempts it so the suite isn't throttled).
"""
from __future__ import annotations

import pytest


def _payload():
    return {"text": "hello world this is a test submission", "author_id": "rl_tester"}


def test_first_ten_submissions_per_minute_succeed(rate_limited_client, mock_judge):
    mock_judge(label="Human", confidence=0.9)
    for i in range(10):
        resp = rate_limited_client.post("/submit", json=_payload())
        assert resp.status_code == 200, f"submission {i+1} unexpectedly throttled"


def test_eleventh_submission_in_a_minute_returns_429(rate_limited_client, mock_judge):
    mock_judge(label="Human", confidence=0.9)
    for _ in range(10):
        assert rate_limited_client.post("/submit", json=_payload()).status_code == 200
    blocked = rate_limited_client.post("/submit", json=_payload())
    assert blocked.status_code == 429
    body = blocked.get_json()
    assert body["error"] == "rate limit exceeded"
    assert "10 per 1 minute" in body["detail"] or "10 per minute" in body["detail"]


def test_rate_limit_only_applies_to_submit_not_log(rate_limited_client, mock_judge):
    """GET /log should NOT be throttled by the /submit limiter."""
    mock_judge(label="Human", confidence=0.9)
    for _ in range(15):
        assert rate_limited_client.get("/log").status_code == 200


def test_appeal_is_not_throttled_by_submit_limiter(rate_limited_client, mock_judge):
    """The /submit limiter is scoped to /submit only — /appeal still goes through.

    (Repeating against the same content would now hit the third-party appeal
    cap, but that's a separate policy enforced in /appeal itself; this test
    just confirms the limiter isn't blocking the endpoint at all.)
    """
    mock_judge(label="Human", confidence=0.9)
    sub = rate_limited_client.post("/submit", json=_payload()).get_json()
    cid = sub["submission_id"]
    resp = rate_limited_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "rate-limit-bypass check",
    })
    assert resp.status_code == 202
