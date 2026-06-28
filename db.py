"""SQLite persistence layer.

M3: `decisions` table with stylometric fields.
M4: adds llm_* / combined_score / signals_agreed / final_label / attribution.
M5: `appeals` and `appeal_evidence` tables; `update_decision_status` helper.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("PROVENANCE_DB_PATH", "provenance.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    submission_id   TEXT PRIMARY KEY,
    author_id       TEXT NOT NULL,
    raw_text        TEXT NOT NULL,
    engine_used     TEXT NOT NULL,
    features        TEXT NOT NULL,   -- JSON: {name: {raw, normalized}}
    stylo_score     REAL NOT NULL,
    llm_label       TEXT,            -- "AI" | "Human" | NULL (judge failed)
    llm_confidence  REAL,            -- judge's confidence in its own label
    llm_rationale   TEXT,            -- judge's free-form reasoning
    llm_ai_score    REAL,            -- unified AI-direction score from judge, or NULL on failure
    combined_score  REAL NOT NULL,   -- (stylo + llm_ai) / 2, or stylo alone on fallback
    signals_agreed  INTEGER NOT NULL,-- 0/1; False on LLM fallback
    final_label     TEXT NOT NULL,   -- "high-confidence AI" | "high-confidence human" | "uncertain"
    attribution     TEXT NOT NULL,   -- "AI" | "human" | "uncertain"
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL    -- ISO-8601 UTC
);

CREATE TABLE IF NOT EXISTS appeals (
    appeal_id          TEXT PRIMARY KEY,
    submission_id      TEXT NOT NULL,
    author_id          TEXT NOT NULL,
    reasoning          TEXT NOT NULL,
    original_decision  TEXT NOT NULL,   -- JSON snapshot {combined_score, attribution, final_label}
    status             TEXT NOT NULL DEFAULT 'under_review',
    created_at         TEXT NOT NULL,   -- ISO-8601 UTC
    FOREIGN KEY (submission_id) REFERENCES decisions(submission_id)
);

CREATE TABLE IF NOT EXISTS appeal_evidence (
    evidence_id    TEXT PRIMARY KEY,
    appeal_id      TEXT NOT NULL,
    filename       TEXT NOT NULL,
    content_type   TEXT NOT NULL,
    captured_at    TEXT NOT NULL,    -- ISO-8601 from the client (when screenshot/doc was captured)
    description    TEXT NOT NULL,
    data           BLOB NOT NULL,
    FOREIGN KEY (appeal_id) REFERENCES appeals(appeal_id)
);
"""

# Columns added after the initial M3 release. Migrate by ALTER on existing DBs.
_M4_COLUMNS = [
    ("llm_label",      "TEXT"),
    ("llm_confidence", "REAL"),
    ("llm_rationale",  "TEXT"),
    ("llm_ai_score",   "REAL"),
    ("combined_score", "REAL"),
    ("signals_agreed", "INTEGER"),
    ("final_label",    "TEXT"),
    ("attribution",    "TEXT"),
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
        for col, decl in _M4_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {decl}")
        conn.commit()


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_decision(
    submission_id: str,
    author_id: str,
    raw_text: str,
    engine_used: str,
    features: dict,
    stylo_score: float,
    *,
    llm_label: str | None,
    llm_confidence: float | None,
    llm_rationale: str | None,
    llm_ai_score: float | None,
    combined_score: float,
    signals_agreed: bool,
    final_label: str,
    attribution: str,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO decisions
                (submission_id, author_id, raw_text, engine_used,
                 features, stylo_score,
                 llm_label, llm_confidence, llm_rationale, llm_ai_score,
                 combined_score, signals_agreed, final_label, attribution,
                 status, created_at)
            VALUES (?, ?, ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?,  'active', ?)
            """,
            (
                submission_id,
                author_id,
                raw_text,
                engine_used,
                json.dumps(features),
                stylo_score,
                llm_label,
                llm_confidence,
                llm_rationale,
                llm_ai_score,
                combined_score,
                1 if signals_agreed else 0,
                final_label,
                attribution,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_decision(submission_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM decisions WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["features"] = json.loads(data["features"])
    return data


def get_decisions(author_id: str | None = None, limit: int = 50) -> list[dict]:
    """Most-recent-first decisions, optionally scoped to a single author."""
    sql = "SELECT * FROM decisions"
    params: tuple = ()
    if author_id is not None:
        sql += " WHERE author_id = ?"
        params = (author_id,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params = params + (limit,)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["features"] = json.loads(data["features"])
        out.append(data)
    return out


# ---- M5: appeals ------------------------------------------------------------

def update_decision_status(submission_id: str, status: str) -> None:
    """Flip a decision's status (e.g. 'active' → 'Under Review'). Score/label
    fields are immutable per planning.md §4 — only `status` is touched."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE decisions SET status = ? WHERE submission_id = ?",
            (status, submission_id),
        )


def insert_appeal(
    appeal_id: str,
    submission_id: str,
    author_id: str,
    reasoning: str,
    original_decision: dict,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO appeals
                (appeal_id, submission_id, author_id, reasoning,
                 original_decision, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'under_review', ?)
            """,
            (
                appeal_id,
                submission_id,
                author_id,
                reasoning,
                json.dumps(original_decision),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def commit_appeal(
    *,
    appeal_id: str,
    submission_id: str,
    author_id: str,
    reasoning: str,
    original_decision: dict,
    evidence_items: list[dict],
    new_status: str,
) -> None:
    """Atomic: insert appeal row, insert evidence rows, flip decision status.

    All three writes share a single transaction. If any insert raises mid-flight
    the whole appeal rolls back — the decision row's status never flips and no
    appeal/evidence rows persist. Prevents the partial-write states we'd
    otherwise see across three separate get_conn() contexts.

    `evidence_items` is a list of dicts shaped:
        {evidence_id, filename, content_type, captured_at, description, data: bytes}
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO appeals
                (appeal_id, submission_id, author_id, reasoning,
                 original_decision, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (appeal_id, submission_id, author_id, reasoning,
             json.dumps(original_decision), new_status, now),
        )
        for ev in evidence_items:
            conn.execute(
                """
                INSERT INTO appeal_evidence
                    (evidence_id, appeal_id, filename, content_type,
                     captured_at, description, data)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ev["evidence_id"], appeal_id, ev["filename"],
                 ev["content_type"], ev["captured_at"], ev["description"],
                 ev["data"]),
            )
        conn.execute(
            "UPDATE decisions SET status = ? WHERE submission_id = ?",
            (new_status, submission_id),
        )


def insert_evidence(
    evidence_id: str,
    appeal_id: str,
    filename: str,
    content_type: str,
    captured_at: str,
    description: str,
    data: bytes,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO appeal_evidence
                (evidence_id, appeal_id, filename, content_type,
                 captured_at, description, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (evidence_id, appeal_id, filename, content_type,
             captured_at, description, data),
        )


def get_appeal(appeal_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM appeals WHERE appeal_id = ?",
            (appeal_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["original_decision"] = json.loads(data["original_decision"])
    return data


def get_evidence_for_appeal(appeal_id: str) -> list[dict]:
    """Evidence attachments for an appeal, sorted by captured_at (oldest first)
    so the reviewer reads progression as a timeline (planning.md §4)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM appeal_evidence WHERE appeal_id = ? ORDER BY captured_at ASC",
            (appeal_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_appeal_for(submission_id: str) -> dict | None:
    """Most-recent appeal against a submission (or None if never appealed).
    Used by /log to surface `appeal_reasoning` alongside the decision row."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM appeals
            WHERE submission_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (submission_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["original_decision"] = json.loads(data["original_decision"])
    return data
