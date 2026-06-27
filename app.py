"""Provenance Guard — Flask service entry point.

M4: `POST /submit` runs Signal 1 (stylometric) + Signal 2 (Groq LLM-as-judge),
combines them via the §1 agreement gate, persists the full decision trace, and
returns {attribution, confidence, label}. `GET /log` reads back the most-recent
entries, optionally scoped to an author.
"""
from __future__ import annotations

import os
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from db import get_decisions, init_db, insert_decision
from signals.combiner import combine
from signals.llm_judge import judge
from signals.stylometric import compute_stylo_score

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


if __name__ == "__main__":
    app.run(debug=True)
