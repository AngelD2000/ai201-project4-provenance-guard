"""Provenance Guard — Flask service entry point.

M4: `POST /submit` runs Signal 1 (stylometric) + Signal 2 (Groq LLM-as-judge),
combines them via the §1 agreement gate, persists the full decision trace, and
returns {attribution, confidence, label}. `GET /log` reads back the most-recent
entries, optionally scoped to an author.

M5: `POST /appeal` lets the original author contest a decision. Verifies the
author matches the submission, accepts an optional list of evidence
attachments (≤10, ≤5 MB each), flips the decision's status to "Under Review"
without mutating any score/label fields.
"""
from __future__ import annotations

import base64
import binascii
import os
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from db import (
    get_decision,
    get_decisions,
    init_db,
    insert_appeal,
    insert_decision,
    insert_evidence,
    update_decision_status,
)
from signals.combiner import combine
from signals.llm_judge import judge
from signals.stylometric import compute_stylo_score

_MAX_ATTACHMENTS = 10
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

load_dotenv()

app = Flask(__name__)
init_db()


@app.post("/submit")
def submit():
    payload = request.get_json()
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    text = payload.get("text")
    author_id = payload.get("author_id")

    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "text is required"}), 400
    if not isinstance(author_id, str) or not author_id.strip():
        return jsonify({"error": "author_id is required"}), 400

    submission_id = str(uuid.uuid4())
    stylo = compute_stylo_score(text)
    judge_result = judge(text)  # None on failure → combiner forces "uncertain"
    decision = combine(
        stylo_score=stylo["stylo_score"],
        llm_ai_score=judge_result["llm_ai_score"] if judge_result else None,
    )

    insert_decision(
        submission_id=submission_id,
        author_id=author_id,
        raw_text=text,
        engine_used=stylo["engine"],
        features=stylo["features"],
        stylo_score=stylo["stylo_score"],
        llm_label=judge_result["llm_label"] if judge_result else None,
        llm_confidence=judge_result["llm_confidence"] if judge_result else None,
        llm_rationale=judge_result["llm_rationale"] if judge_result else None,
        llm_ai_score=judge_result["llm_ai_score"] if judge_result else None,
        combined_score=decision["combined_score"],
        signals_agreed=decision["signals_agreed"],
        final_label=decision["final_label"],
        attribution=decision["attribution"],
    )

    return jsonify({
        "submission_id": submission_id,
        "attribution":   decision["attribution"],
        "confidence":    decision["combined_score"],
        "label":         decision["final_label"],
    }), 200


@app.get("/log")
def log():
    """Recent audit-log entries. Optional `author_id` filters; `limit` caps at 200."""
    author_id = request.args.get("author_id")
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    limit = max(1, min(limit, 200))

    rows = get_decisions(author_id=author_id, limit=limit)
    entries = [
        {
            "content_id":     r["submission_id"],
            "creator_id":     r["author_id"],
            "timestamp":      r["created_at"],
            "engine":         r["engine_used"],
            "stylo_score":    r["stylo_score"],
            "llm_score":      r["llm_ai_score"],
            "attribution":    r["attribution"],
            "confidence":     r["combined_score"],
            "label":          r["final_label"],
            "signals_agreed": bool(r["signals_agreed"]) if r["signals_agreed"] is not None else None,
            "status":         r["status"],
        }
        for r in rows
    ]
    return jsonify({"entries": entries}), 200


@app.post("/appeal")
def appeal():
    """Author-contested appeal against a /submit decision.

    Validation order (matters for the right status codes):
      1. Body shape + required fields → 400
      2. Evidence schema + caps → 400 / 413
      3. Submission lookup → 404
      4. Author match → 403
      5. Persist appeal + evidence, flip decision status → 202
    """
    payload = request.get_json()
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    submission_id = payload.get("submission_id")
    author_id     = payload.get("author_id")
    reasoning     = payload.get("reasoning")
    evidence      = payload.get("evidence", [])

    if not isinstance(submission_id, str) or not submission_id.strip():
        return jsonify({"error": "submission_id is required"}), 400
    if not isinstance(author_id, str) or not author_id.strip():
        return jsonify({"error": "author_id is required"}), 400
    if not isinstance(reasoning, str) or not reasoning.strip():
        return jsonify({"error": "reasoning is required"}), 400
    if not isinstance(evidence, list):
        return jsonify({"error": "evidence must be an array"}), 400

    # Evidence caps + schema. Decode base64 here so we can size-check the real
    # payload (not the base64-inflated length).
    if len(evidence) > _MAX_ATTACHMENTS:
        return jsonify({
            "error": f"too many attachments (max {_MAX_ATTACHMENTS})"
        }), 413

    decoded_attachments = []
    for att in evidence:
        if not isinstance(att, dict):
            return jsonify({"error": "each evidence item must be an object"}), 400
        for field in ("filename", "content_type", "captured_at", "description", "data"):
            v = att.get(field)
            if not isinstance(v, str) or not v.strip():
                return jsonify({"error": f"evidence.{field} is required"}), 400
        try:
            blob = base64.b64decode(att["data"], validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "evidence.data must be valid base64"}), 400
        if len(blob) > _MAX_ATTACHMENT_BYTES:
            return jsonify({
                "error": f"attachment exceeds {_MAX_ATTACHMENT_BYTES} bytes"
            }), 413
        decoded_attachments.append((att, blob))

    # Lookup + authorization.
    original = get_decision(submission_id)
    if original is None:
        return jsonify({"error": "submission not found"}), 404
    if original["author_id"] != author_id:
        return jsonify({"error": "author_id does not match submission"}), 403

    # Persist. Decision row stays immutable except for `status`.
    appeal_id = str(uuid.uuid4())
    insert_appeal(
        appeal_id=appeal_id,
        submission_id=submission_id,
        author_id=author_id,
        reasoning=reasoning,
        original_decision={
            "combined_score": original["combined_score"],
            "attribution":    original["attribution"],
            "final_label":    original["final_label"],
        },
    )
    for att, blob in decoded_attachments:
        insert_evidence(
            evidence_id=str(uuid.uuid4()),
            appeal_id=appeal_id,
            filename=att["filename"],
            content_type=att["content_type"],
            captured_at=att["captured_at"],
            description=att["description"],
            data=blob,
        )
    update_decision_status(submission_id, "Under Review")

    return jsonify({
        "submission_id": submission_id,
        "appeal_id":     appeal_id,
        "status":        "Under Review",
    }), 202


if __name__ == "__main__":
    app.run(debug=True)
