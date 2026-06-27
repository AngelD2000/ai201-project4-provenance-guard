"""Shared pytest fixtures.

Sets PROVENANCE_DB_PATH to a temp file *before* db / app are imported, so the
real provenance.db is never touched by tests. Per-test fixtures wipe the file
so each test sees a clean DB.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make project root importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Critical: must be set before any test module imports `db` or `app`.
_TEST_DB = os.path.join(tempfile.gettempdir(), "provenance_test.db")
os.environ["PROVENANCE_DB_PATH"] = _TEST_DB


@pytest.fixture
def fresh_db():
    """Yield the db module against a freshly-reinitialized empty database."""
    import db
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()
    yield db
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    """Flask test client backed by a fresh DB and a mockable judge."""
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


@pytest.fixture
def mock_judge(monkeypatch):
    """Returns a setter — call mock_judge(label, confidence) to install a stub."""
    def install(label: str = "AI", confidence: float = 0.9, rationale: str = "stub"):
        llm_ai_score = confidence if label == "AI" else 1.0 - confidence
        def fake(_text):
            return {
                "llm_label":      label,
                "llm_confidence": confidence,
                "llm_rationale":  rationale,
                "llm_ai_score":   llm_ai_score,
            }
        monkeypatch.setattr("app.judge", fake)
    return install


@pytest.fixture
def mock_judge_failure(monkeypatch):
    """Install a judge stub that simulates failure (returns None)."""
    monkeypatch.setattr("app.judge", lambda _text: None)
