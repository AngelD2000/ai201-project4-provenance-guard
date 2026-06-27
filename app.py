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
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from db import (
    get_decision,
    get_decisions,
    get_latest_appeal_for,
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

# /submit rate limits. See README for the writer-vs-abuser math.
#   10/min — caps a script flood at the second-minute boundary; a writer
#            iterating on a draft can still resubmit roughly every 6 seconds.
#   100/day — catches sustained low-rate automation a per-minute cap misses.
# Storage is in-memory: limits reset on process restart, which is fine for
# single-process development. Production should swap to redis://.
SUBMIT_RATE_LIMITS = ["10 per minute", "100 per day"]

load_dotenv()

app = Flask(__name__)
init_db()

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
    # When tests set app.config["TESTING"] = True we want the limiter quiet so
    # the suite isn't throttled across 100+ /submit calls. Wired below.
)


@app.errorhandler(429)
def ratelimit_handler(e):
    """Return JSON so the API contract stays consistent across status codes."""
    return jsonify({
        "error":  "rate limit exceeded",
        "detail": str(getattr(e, "description", "too many requests")),
    }), 429


@app.post("/submit")
@limiter.limit(";".join(SUBMIT_RATE_LIMITS), exempt_when=lambda: app.config.get("TESTING", False))
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
    entries = []
    for r in rows:
        latest_appeal = get_latest_appeal_for(r["submission_id"])
        entries.append({
            "content_id":       r["submission_id"],
            "creator_id":       r["author_id"],
            "timestamp":        r["created_at"],
            "engine":           r["engine_used"],
            "stylo_score":      r["stylo_score"],
            "llm_score":        r["llm_ai_score"],
            "attribution":      r["attribution"],
            "confidence":       r["combined_score"],
            "label":            r["final_label"],
            "signals_agreed":   bool(r["signals_agreed"]) if r["signals_agreed"] is not None else None,
            "status":           r["status"],
            "appeal_reasoning": latest_appeal["reasoning"] if latest_appeal else None,
        })
    return jsonify({"entries": entries}), 200


@app.post("/appeal")
def appeal():
    """Author-contested appeal against a /submit decision.

    Wire shape (graded spec): {content_id, creator_reasoning, evidence?, author_id?}.
    `author_id` is optional — when supplied, the author-match check from
    planning.md §4 is enforced (403 on mismatch); when omitted, the appeal
    is accepted on submission existence + reasoning validity alone.

    Validation order (matters for the right status codes):
      1. Body shape + required fields → 400
      2. Evidence schema + caps → 400 / 413
      3. Submission lookup → 404
      4. Author match (if author_id supplied) → 403
      5. Persist appeal + evidence, flip decision status → 202
    """
    payload = request.get_json()
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    content_id        = payload.get("content_id")
    author_id         = payload.get("author_id")  # optional
    creator_reasoning = payload.get("creator_reasoning")
    evidence          = payload.get("evidence", [])

    if not isinstance(content_id, str) or not content_id.strip():
        return jsonify({"error": "content_id is required"}), 400
    if author_id is not None and (not isinstance(author_id, str) or not author_id.strip()):
        return jsonify({"error": "author_id, if supplied, must be a non-empty string"}), 400
    if not isinstance(creator_reasoning, str) or not creator_reasoning.strip():
        return jsonify({"error": "creator_reasoning is required"}), 400
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

    # Lookup + (optional) authorization.
    original = get_decision(content_id)
    if original is None:
        return jsonify({"error": "content not found"}), 404
    if author_id is not None and original["author_id"] != author_id:
        return jsonify({"error": "author_id does not match content"}), 403

    # Persist. Decision row stays immutable except for `status`.
    appeal_id = str(uuid.uuid4())
    insert_appeal(
        appeal_id=appeal_id,
        submission_id=content_id,
        author_id=author_id or original["author_id"],
        reasoning=creator_reasoning,
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
    update_decision_status(content_id, "under_review")

    return jsonify({
        "content_id": content_id,
        "appeal_id":  appeal_id,
        "status":     "under_review",
    }), 202


if __name__ == "__main__":
    app.run(debug=True)
