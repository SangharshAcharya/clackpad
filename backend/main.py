"""
Clackpad leaderboard backend.

A tiny FastAPI service that stores one best daily-challenge score per
(name, date, script) and serves a leaderboard for a given day. This is the
whole "multi-user" surface for now — everything else in Clackpad (streaks,
lessons, ghost races, badges) still lives in each browser's localStorage.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload

Deploy: see ../DEPLOY.md
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("CLACKPAD_DB_PATH", "clackpad.db")

app = FastAPI(title="Clackpad Leaderboard API")

# Wide-open CORS: this API only ever stores a name + wpm + accuracy for a
# public daily leaderboard, so there's nothing sensitive to protect behind
# an origin check. Tighten this to your actual site's origin if you'd rather.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_scores (
                name TEXT NOT NULL,
                script TEXT NOT NULL,
                date TEXT NOT NULL,
                wpm INTEGER NOT NULL,
                acc INTEGER NOT NULL,
                submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (name, script, date)
            )
            """
        )


init_db()


class ScoreSubmission(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    script: str = Field(pattern="^(en|ne)$")
    date: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD
    wpm: int = Field(ge=0, le=400)
    acc: int = Field(ge=0, le=100)


def _clean_name(name: str) -> str:
    # Keep it plain: strip control characters, collapse whitespace, cap length.
    cleaned = "".join(ch for ch in name if ch.isprintable()).strip()
    return cleaned[:20] or "anonymous"


@app.get("/api/health")
def health():
    return {"ok": True, "today": date.today().isoformat()}


@app.post("/api/daily-score")
def submit_score(payload: ScoreSubmission):
    name = _clean_name(payload.name)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT wpm FROM daily_scores WHERE name = ? AND script = ? AND date = ?",
            (name, payload.script, payload.date),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO daily_scores (name, script, date, wpm, acc) VALUES (?, ?, ?, ?, ?)",
                (name, payload.script, payload.date, payload.wpm, payload.acc),
            )
        elif payload.wpm > existing["wpm"]:
            # Only the first attempt is meant to be "official" client-side,
            # but if a higher score ever does arrive, keep the best one.
            conn.execute(
                "UPDATE daily_scores SET wpm = ?, acc = ? WHERE name = ? AND script = ? AND date = ?",
                (payload.wpm, payload.acc, name, payload.script, payload.date),
            )
    return {"ok": True}


@app.get("/api/daily-leaderboard")
def leaderboard(date: str, script: str = "en", limit: int = 10):
    if script not in ("en", "ne"):
        raise HTTPException(400, "script must be 'en' or 'ne'")
    limit = max(1, min(limit, 50))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT name, wpm, acc FROM daily_scores
            WHERE date = ? AND script = ?
            ORDER BY wpm DESC
            LIMIT ?
            """,
            (date, script, limit),
        ).fetchall()
    return [{"name": r["name"], "wpm": r["wpm"], "acc": r["acc"]} for r in rows]
