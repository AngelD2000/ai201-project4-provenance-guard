"""POST /appeal tests (planning.md §4).

Covers every status-code branch (202, 400, 403, 404, 413), the immutability
guarantee (decision score/label fields don't change), and the status flip
(`active` → `Under Review`).
"""
from __future__ import annotations

import base64

import pytest

from tests.samples import HUMAN_PARAGRAPH


def _submit_text(client, mock_judge, text: str = HUMAN_PARAGRAPH, author: str = "alice"):
    """Helper: drop a submission so we have something to appeal against."""
    mock_judge(label="Human", confidence=0.9)
    resp = client.post("/submit", json={"text": text, "author_id": author})
    assert resp.status_code == 200
    return resp.get_json()["submission_id"]


def _att(filename="proof.png", content_type="image/png",
         captured_at="2026-06-20T10:00:00Z", description="draft v1",
         data: bytes = b"PNGFAKEDATA"):
    return {
        "filename":     filename,
        "content_type": content_type,
        "captured_at":  captured_at,
        "description":  description,
        "data":         base64.b64encode(data).decode(),
    }


# ---- 202 happy path -------------------------------------------------------

def test_appeal_succeeds_with_matching_author_and_reasoning(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "submission_id": sid,
        "author_id":     "alice",
        "reasoning":     "this is actually my draft, see attached",
    })
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["submission_id"] == sid
    assert body["status"] == "Under Review"
    assert "appeal_id" in body


def test_appeal_flips_decision_status_without_mutating_scores(app_client, mock_judge, fresh_db):
    sid = _submit_text(app_client, mock_judge)
    pre = fresh_db.get_decision(sid)

    app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice", "reasoning": "see drafts",
    })

    post = fresh_db.get_decision(sid)
    assert post["status"] == "Under Review"
    # Every score/label field must be unchanged.
    for key in ("stylo_score", "llm_ai_score", "combined_score",
                "final_label", "attribution", "signals_agreed"):
        assert post[key] == pre[key], f"{key} was mutated by /appeal"


def test_appeal_persists_original_decision_snapshot(app_client, mock_judge, fresh_db):
    import sqlite3, json
    sid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice", "reasoning": "see drafts",
    }).get_json()

    conn = sqlite3.connect(fresh_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM appeals WHERE appeal_id = ?",
                       (resp["appeal_id"],)).fetchone()
    conn.close()
    assert row is not None
    snap = json.loads(row["original_decision"])
    assert "combined_score" in snap
    assert "attribution" in snap
    assert "final_label" in snap


def test_appeal_accepts_evidence_within_caps(app_client, mock_judge, fresh_db):
    sid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice",
        "reasoning": "see attached drafts",
        "evidence": [_att(description="early draft"), _att(description="later draft")],
    })
    assert resp.status_code == 202

    appeal_id = resp.get_json()["appeal_id"]
    evidence = fresh_db.get_evidence_for_appeal(appeal_id)
    assert len(evidence) == 2
    assert {e["description"] for e in evidence} == {"early draft", "later draft"}


def test_appeal_evidence_sorted_by_captured_at(app_client, mock_judge, fresh_db):
    """Reviewer reads progression as a timeline (§4)."""
    sid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice", "reasoning": "x",
        "evidence": [
            _att(captured_at="2026-06-25T10:00:00Z", description="final"),
            _att(captured_at="2026-06-20T10:00:00Z", description="draft"),
            _att(captured_at="2026-06-23T10:00:00Z", description="revision"),
        ],
    }).get_json()
    evidence = fresh_db.get_evidence_for_appeal(resp["appeal_id"])
    assert [e["description"] for e in evidence] == ["draft", "revision", "final"]


# ---- 400 validation -------------------------------------------------------

def test_appeal_empty_reasoning_returns_400(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice", "reasoning": "   ",
    })
    assert resp.status_code == 400


def test_appeal_missing_submission_id_returns_400(app_client):
    resp = app_client.post("/appeal", json={"author_id": "a", "reasoning": "r"})
    assert resp.status_code == 400


def test_appeal_missing_author_returns_400(app_client):
    resp = app_client.post("/appeal", json={"submission_id": "s", "reasoning": "r"})
    assert resp.status_code == 400


def test_appeal_evidence_not_a_list_returns_400(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice",
        "reasoning": "x", "evidence": "not a list",
    })
    assert resp.status_code == 400


def test_appeal_evidence_missing_field_returns_400(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge)
    bad = _att()
    del bad["filename"]
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice",
        "reasoning": "x", "evidence": [bad],
    })
    assert resp.status_code == 400


def test_appeal_evidence_invalid_base64_returns_400(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge)
    bad = _att()
    bad["data"] = "not!!base64@@"
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice",
        "reasoning": "x", "evidence": [bad],
    })
    assert resp.status_code == 400


# ---- 403 / 404 ------------------------------------------------------------

def test_appeal_unknown_submission_returns_404(app_client):
    resp = app_client.post("/appeal", json={
        "submission_id": "does-not-exist", "author_id": "alice", "reasoning": "x",
    })
    assert resp.status_code == 404


def test_appeal_wrong_author_returns_403(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge, author="alice")
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "mallory", "reasoning": "give me",
    })
    assert resp.status_code == 403


# ---- 413 evidence caps ----------------------------------------------------

def test_appeal_too_many_attachments_returns_413(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice", "reasoning": "x",
        "evidence": [_att() for _ in range(11)],
    })
    assert resp.status_code == 413


def test_appeal_oversized_attachment_returns_413(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge)
    huge = b"x" * (5 * 1024 * 1024 + 1)  # 5 MB + 1 byte
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice", "reasoning": "x",
        "evidence": [_att(data=huge)],
    })
    assert resp.status_code == 413


def test_appeal_exactly_max_attachments_is_accepted(app_client, mock_judge):
    sid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "submission_id": sid, "author_id": "alice", "reasoning": "x",
        "evidence": [_att() for _ in range(10)],
    })
    assert resp.status_code == 202
