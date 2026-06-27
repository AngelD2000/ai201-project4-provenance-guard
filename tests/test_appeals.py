"""POST /appeal tests.

Wire shape: {content_id, creator_reasoning, evidence?, author_id?}.
`author_id` is optional — enforced when supplied (planning.md §4 security
model), skipped when omitted (graded-spec minimal shape).

Covers every status-code branch (202, 400, 403, 404, 413), the immutability
guarantee (decision score/label fields don't change), the status flip
(`active` → `under_review`), and the timeline-sorted evidence read.
"""
from __future__ import annotations

import base64

import pytest

from tests.samples import HUMAN_PARAGRAPH


def _submit_text(client, mock_judge, text: str = HUMAN_PARAGRAPH, author: str = "alice"):
    """Drop a submission so we have a content_id to appeal against."""
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

def test_appeal_succeeds_with_minimal_graded_shape(app_client, mock_judge):
    """Graded spec: just content_id + creator_reasoning, no author_id."""
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id":        cid,
        "creator_reasoning": "I wrote this myself; my voice can read formal.",
    })
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["content_id"] == cid
    assert body["status"] == "under_review"
    assert "appeal_id" in body


def test_appeal_succeeds_with_matching_author_id(app_client, mock_judge):
    """Optional author_id, when supplied, must match the decision."""
    cid = _submit_text(app_client, mock_judge, author="alice")
    resp = app_client.post("/appeal", json={
        "content_id":        cid,
        "author_id":         "alice",
        "creator_reasoning": "this is actually my draft, see attached",
    })
    assert resp.status_code == 202


def test_appeal_flips_decision_status_without_mutating_scores(app_client, mock_judge, fresh_db):
    cid = _submit_text(app_client, mock_judge)
    pre = fresh_db.get_decision(cid)

    app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "see drafts",
    })

    post = fresh_db.get_decision(cid)
    assert post["status"] == "under_review"
    for key in ("stylo_score", "llm_ai_score", "combined_score",
                "final_label", "attribution", "signals_agreed"):
        assert post[key] == pre[key], f"{key} was mutated by /appeal"


def test_appeal_persists_original_decision_snapshot(app_client, mock_judge, fresh_db):
    import sqlite3, json
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "see drafts",
    }).get_json()

    conn = sqlite3.connect(fresh_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM appeals WHERE appeal_id = ?",
                       (resp["appeal_id"],)).fetchone()
    conn.close()
    assert row is not None
    snap = json.loads(row["original_decision"])
    assert {"combined_score", "attribution", "final_label"} <= set(snap)


def test_appeal_accepts_evidence_within_caps(app_client, mock_judge, fresh_db):
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id":        cid,
        "creator_reasoning": "see attached drafts",
        "evidence":          [_att(description="early draft"), _att(description="later draft")],
    })
    assert resp.status_code == 202

    appeal_id = resp.get_json()["appeal_id"]
    evidence = fresh_db.get_evidence_for_appeal(appeal_id)
    assert len(evidence) == 2
    assert {e["description"] for e in evidence} == {"early draft", "later draft"}


def test_appeal_evidence_sorted_by_captured_at(app_client, mock_judge, fresh_db):
    """Reviewer reads progression as a timeline (§4)."""
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "x",
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
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "   ",
    })
    assert resp.status_code == 400


def test_appeal_missing_content_id_returns_400(app_client):
    resp = app_client.post("/appeal", json={"creator_reasoning": "r"})
    assert resp.status_code == 400


def test_appeal_missing_reasoning_returns_400(app_client):
    resp = app_client.post("/appeal", json={"content_id": "s"})
    assert resp.status_code == 400


def test_appeal_invalid_author_id_type_returns_400(app_client, mock_judge):
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "r", "author_id": 42,
    })
    assert resp.status_code == 400


def test_appeal_evidence_not_a_list_returns_400(app_client, mock_judge):
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "x", "evidence": "not a list",
    })
    assert resp.status_code == 400


def test_appeal_evidence_missing_field_returns_400(app_client, mock_judge):
    cid = _submit_text(app_client, mock_judge)
    bad = _att()
    del bad["filename"]
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "x", "evidence": [bad],
    })
    assert resp.status_code == 400


def test_appeal_evidence_invalid_base64_returns_400(app_client, mock_judge):
    cid = _submit_text(app_client, mock_judge)
    bad = _att()
    bad["data"] = "not!!base64@@"
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "x", "evidence": [bad],
    })
    assert resp.status_code == 400


# ---- 403 / 404 ------------------------------------------------------------

def test_appeal_unknown_content_returns_404(app_client):
    resp = app_client.post("/appeal", json={
        "content_id": "does-not-exist", "creator_reasoning": "x",
    })
    assert resp.status_code == 404


def test_appeal_wrong_author_returns_403_when_author_supplied(app_client, mock_judge):
    cid = _submit_text(app_client, mock_judge, author="alice")
    resp = app_client.post("/appeal", json={
        "content_id": cid, "author_id": "mallory", "creator_reasoning": "give me",
    })
    assert resp.status_code == 403


# ---- 413 evidence caps ----------------------------------------------------

def test_appeal_too_many_attachments_returns_413(app_client, mock_judge):
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "x",
        "evidence": [_att() for _ in range(11)],
    })
    assert resp.status_code == 413


def test_appeal_oversized_attachment_returns_413(app_client, mock_judge):
    cid = _submit_text(app_client, mock_judge)
    huge = b"x" * (5 * 1024 * 1024 + 1)  # 5 MB + 1 byte
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "x",
        "evidence": [_att(data=huge)],
    })
    assert resp.status_code == 413


def test_appeal_exactly_max_attachments_is_accepted(app_client, mock_judge):
    cid = _submit_text(app_client, mock_judge)
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": "x",
        "evidence": [_att() for _ in range(10)],
    })
    assert resp.status_code == 202


# ---- /log integration ----------------------------------------------------

def test_log_surfaces_appeal_reasoning_after_appeal(app_client, mock_judge):
    """After /appeal, the /log entry shows status="under_review" and the
    appeal_reasoning field populated (graded-spec verification)."""
    cid = _submit_text(app_client, mock_judge)
    reasoning = "I am a non-native English speaker; my voice reads formal."
    resp = app_client.post("/appeal", json={
        "content_id": cid, "creator_reasoning": reasoning,
    })
    assert resp.status_code == 202

    log = app_client.get("/log").get_json()["entries"]
    entry = next(e for e in log if e["content_id"] == cid)
    assert entry["status"] == "under_review"
    assert entry["appeal_reasoning"] == reasoning


def test_log_appeal_reasoning_is_null_when_not_appealed(app_client, mock_judge):
    _submit_text(app_client, mock_judge)
    entry = app_client.get("/log").get_json()["entries"][0]
    assert entry["status"] == "active"
    assert entry["appeal_reasoning"] is None
