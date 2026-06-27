"""SQLite persistence-layer tests. Uses the `fresh_db` fixture for isolation."""
from __future__ import annotations

import sqlite3
import time

import pytest


# Minimal payload all tests insert; per-test overrides via dict merge.
_BASE = {
    "raw_text":       "hello world",
    "engine_used":    "essay",
    "features":       {"burstiness_score": {"raw": 5.0, "normalized": 0.5}},
    "stylo_score":    0.5,
    "llm_label":      "AI",
    "llm_confidence": 0.8,
    "llm_rationale":  "uniform cadence",
    "llm_ai_score":   0.8,
    "combined_score": 0.65,
    "signals_agreed": True,
    "final_label":    "uncertain",
    "attribution":    "uncertain",
}


def _insert(db, submission_id: str, author_id: str, **overrides):
    payload = {**_BASE, **overrides}
    db.insert_decision(submission_id=submission_id, author_id=author_id, **payload)


# ---- Schema -----------------------------------------------------------------

def test_init_db_creates_all_expected_columns(fresh_db):
    conn = sqlite3.connect(fresh_db.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    conn.close()
    expected = {
        "submission_id", "author_id", "raw_text", "engine_used",
        "features", "stylo_score",
        "llm_label", "llm_confidence", "llm_rationale", "llm_ai_score",
        "combined_score", "signals_agreed", "final_label", "attribution",
        "status", "created_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_init_db_is_idempotent(fresh_db):
    fresh_db.init_db()  # second call should not raise
    fresh_db.init_db()


def test_m3_legacy_db_gets_m4_columns_via_alter(fresh_db):
    """Simulate an old M3 DB (no M4 columns) and confirm init_db ALTERs in place."""
    # Drop the table, recreate it with only M3 columns, re-run init_db.
    conn = sqlite3.connect(fresh_db.DB_PATH)
    conn.execute("DROP TABLE decisions")
    conn.execute("""
        CREATE TABLE decisions (
            submission_id TEXT PRIMARY KEY,
            author_id     TEXT NOT NULL,
            raw_text      TEXT NOT NULL,
            engine_used   TEXT NOT NULL,
            features      TEXT NOT NULL,
            stylo_score   REAL NOT NULL,
            status        TEXT NOT NULL DEFAULT 'active',
            created_at    TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    fresh_db.init_db()

    conn = sqlite3.connect(fresh_db.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    conn.close()
    for m4_col in ("llm_label", "llm_confidence", "llm_rationale", "llm_ai_score",
                   "combined_score", "signals_agreed", "final_label", "attribution"):
        assert m4_col in cols


# ---- Insert / get round-trip -----------------------------------------------

def test_insert_and_get_round_trip(fresh_db):
    _insert(fresh_db, "sid1", "alice")
    row = fresh_db.get_decision("sid1")
    assert row["submission_id"] == "sid1"
    assert row["author_id"] == "alice"
    assert row["llm_label"] == "AI"
    assert row["signals_agreed"] == 1  # SQLite int
    assert row["status"] == "active"


def test_get_decision_parses_features_json(fresh_db):
    _insert(fresh_db, "sid1", "alice")
    row = fresh_db.get_decision("sid1")
    assert row["features"]["burstiness_score"]["raw"] == 5.0
    assert row["features"]["burstiness_score"]["normalized"] == 0.5


def test_get_decision_returns_none_for_unknown_id(fresh_db):
    assert fresh_db.get_decision("does-not-exist") is None


def test_insert_persists_null_llm_fields_on_judge_failure(fresh_db):
    _insert(
        fresh_db, "sid1", "alice",
        llm_label=None, llm_confidence=None, llm_rationale=None, llm_ai_score=None,
        signals_agreed=False, combined_score=0.92,
    )
    row = fresh_db.get_decision("sid1")
    assert row["llm_label"] is None
    assert row["llm_ai_score"] is None
    assert row["combined_score"] == 0.92


# ---- get_decisions: ordering, filtering, limit -----------------------------

def test_get_decisions_orders_newest_first(fresh_db):
    for i in range(3):
        _insert(fresh_db, f"s{i}", "alice")
        time.sleep(0.005)  # ensure distinct ISO timestamps

    ids = [r["submission_id"] for r in fresh_db.get_decisions()]
    assert ids == ["s2", "s1", "s0"]


def test_get_decisions_filters_by_author(fresh_db):
    for sid, auth in [("a", "alice"), ("b", "bob"), ("c", "alice")]:
        _insert(fresh_db, sid, auth)
    rows = fresh_db.get_decisions(author_id="alice")
    assert {r["submission_id"] for r in rows} == {"a", "c"}


def test_get_decisions_respects_limit(fresh_db):
    for i in range(5):
        _insert(fresh_db, f"s{i}", "alice")
    assert len(fresh_db.get_decisions(limit=2)) == 2


def test_get_decisions_unknown_author_returns_empty(fresh_db):
    _insert(fresh_db, "s1", "alice")
    assert fresh_db.get_decisions(author_id="nobody") == []


def test_get_decisions_empty_db_returns_empty_list(fresh_db):
    assert fresh_db.get_decisions() == []
