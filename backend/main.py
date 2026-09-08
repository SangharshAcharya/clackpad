"""
Clackpad leaderboard backend.

A tiny FastAPI service that stores best typing scores for Clackpad's three
online-leaderboard features:

  - "daily"     the daily challenge (one official attempt per person/day)
  - "speedtest" the speed test tab (unlimited practice)
  - "ghost"     ghost race mode

For each (name, script, mode, board, duration) combination we track two things:
  - the best score *for a given date* (a "daily" board, reset each day)
  - the best score *of all time*      (an "all-time" high-score board)

`duration` only matters for the speed test board (15/30/60/120 seconds) —
daily challenge and ghost race submissions always send duration=0, since a
15-second sprint and a 2-minute run aren't a fair comparison, but daily
challenge/ghost race don't have that axis at all.

Everything else in Clackpad (streaks, lessons, badges, the ghost you race
against) still lives in each browser's localStorage — this API only ever
sees a name + wpm + accuracy, submitted once a run finishes.

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

# Wide-open CORS: this API only ever stores a name + wpm + accuracy for
# public leaderboards, so there's nothing sensitive to protect behind an
# origin check. Tighten this to your actual site's origin if you'd rather.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BOARDS = ("daily", "speedtest", "ghost")
MODES = ("words", "sentences", "paragraph")
SCRIPTS = ("en", "ne")

_DAILY_SCHEMA = """
    CREATE TABLE IF NOT EXISTS daily_scores (
        name TEXT NOT NULL,
        script TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'sentences',
        board TEXT NOT NULL DEFAULT 'daily',
        duration INTEGER NOT NULL DEFAULT 0,
        date TEXT NOT NULL,
        wpm INTEGER NOT NULL,
        acc INTEGER NOT NULL,
        submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (name, script, mode, board, duration, date)
    )
"""

_ALLTIME_SCHEMA = """
    CREATE TABLE IF NOT EXISTS alltime_scores (
        name TEXT NOT NULL,
        script TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'sentences',
        board TEXT NOT NULL DEFAULT 'daily',
        duration INTEGER NOT NULL DEFAULT 0,
        wpm INTEGER NOT NULL,
        acc INTEGER NOT NULL,
        submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (name, script, mode, board, duration)
    )
"""


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
        cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(daily_scores)").fetchall()
        ]
        if cols and ("board" not in cols or "duration" not in cols):
            # Pre-existing DB from before boards/duration existed. Migrate it
            # onto the current primary key (which now also includes
            # `duration`, so speed-test scores at different time limits
            # don't collide with or get compared against each other).
            conn.execute("ALTER TABLE daily_scores RENAME TO daily_scores_old")
            conn.execute(_DAILY_SCHEMA)
            old_cols = set(cols)
            board_expr = "board" if "board" in old_cols else "'daily'"
            duration_expr = "duration" if "duration" in old_cols else "0"
            conn.execute(
                f"""
                INSERT INTO daily_scores (name, script, mode, board, duration, date, wpm, acc, submitted_at)
                SELECT name, script, mode, {board_expr}, {duration_expr}, date, wpm, acc, submitted_at FROM daily_scores_old
                """
            )
            conn.execute("DROP TABLE daily_scores_old")
        else:
            conn.execute(_DAILY_SCHEMA)
        conn.execute(_ALLTIME_SCHEMA)


init_db()

_BOARD_PATTERN = "^(" + "|".join(BOARDS) + ")$"
_MODE_PATTERN = "^(" + "|".join(MODES) + ")$"
_SCRIPT_PATTERN = "^(" + "|".join(SCRIPTS) + ")$"


class ScoreSubmission(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    script: str = Field(pattern=_SCRIPT_PATTERN)
    mode: str = Field(default="sentences", pattern=_MODE_PATTERN)
    board: str = Field(default="daily", pattern=_BOARD_PATTERN)
    duration: int = Field(default=0, ge=0, le=600)  # seconds; 0 where duration doesn't apply (daily/ghost)
    date: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD
    wpm: int = Field(ge=0, le=400)
    acc: int = Field(ge=0, le=100)


def _clean_name(name: str) -> str:
    # Keep it plain: strip control characters, collapse whitespace, cap length.
    cleaned = "".join(ch for ch in name if ch.isprintable()).strip()
    return cleaned[:20] or "anonymous"


def _check_board(board: str) -> None:
    if board not in BOARDS:
        raise HTTPException(400, f"board must be one of {BOARDS}")


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise HTTPException(400, f"mode must be one of {MODES}")


def _check_script(script: str) -> None:
    if script not in SCRIPTS:
        raise HTTPException(400, f"script must be one of {SCRIPTS}")


@app.get("/api/health")
def health():
    return {"ok": True, "today": date.today().isoformat()}


@app.post("/api/score")
def submit_score(payload: ScoreSubmission):
    name = _clean_name(payload.name)
    with get_db() as conn:
        # Best score for that specific day (the "daily" board, reset each day).
        existing = conn.execute(
            "SELECT wpm FROM daily_scores WHERE name = ? AND script = ? AND mode = ? AND board = ? AND duration = ? AND date = ?",
            (name, payload.script, payload.mode, payload.board, payload.duration, payload.date),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO daily_scores (name, script, mode, board, duration, date, wpm, acc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, payload.script, payload.mode, payload.board, payload.duration, payload.date, payload.wpm, payload.acc),
            )
        elif payload.wpm > existing["wpm"]:
            conn.execute(
                "UPDATE daily_scores SET wpm = ?, acc = ? WHERE name = ? AND script = ? AND mode = ? AND board = ? AND duration = ? AND date = ?",
                (payload.wpm, payload.acc, name, payload.script, payload.mode, payload.board, payload.duration, payload.date),
            )

        # Best score ever (the "all-time" board).
        best = conn.execute(
            "SELECT wpm FROM alltime_scores WHERE name = ? AND script = ? AND mode = ? AND board = ? AND duration = ?",
            (name, payload.script, payload.mode, payload.board, payload.duration),
        ).fetchone()
        if best is None:
            conn.execute(
                "INSERT INTO alltime_scores (name, script, mode, board, duration, wpm, acc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, payload.script, payload.mode, payload.board, payload.duration, payload.wpm, payload.acc),
            )
        elif payload.wpm > best["wpm"]:
            conn.execute(
                "UPDATE alltime_scores SET wpm = ?, acc = ? WHERE name = ? AND script = ? AND mode = ? AND board = ? AND duration = ?",
                (payload.wpm, payload.acc, name, payload.script, payload.mode, payload.board, payload.duration),
            )
    return {"ok": True}


@app.get("/api/leaderboard")
def leaderboard(date: str, script: str = "en", mode: str = "sentences", board: str = "daily", duration: int = 0, limit: int = 10):
    """Best score per person for one specific day."""
    _check_script(script)
    _check_mode(mode)
    _check_board(board)
    limit = max(1, min(limit, 50))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT name, wpm, acc FROM daily_scores
            WHERE date = ? AND script = ? AND mode = ? AND board = ? AND duration = ?
            ORDER BY wpm DESC
            LIMIT ?
            """,
            (date, script, mode, board, duration, limit),
        ).fetchall()
    return [{"name": r["name"], "wpm": r["wpm"], "acc": r["acc"]} for r in rows]


@app.get("/api/leaderboard/alltime")
def alltime_leaderboard(script: str = "en", mode: str = "sentences", board: str = "daily", duration: int = 0, limit: int = 10):
    """Best score per person ever, for a given board/script/mode/duration."""
    _check_script(script)
    _check_mode(mode)
    _check_board(board)
    limit = max(1, min(limit, 50))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT name, wpm, acc FROM alltime_scores
            WHERE script = ? AND mode = ? AND board = ? AND duration = ?
            ORDER BY wpm DESC
            LIMIT ?
            """,
            (script, mode, board, duration, limit),
        ).fetchall()
    return [{"name": r["name"], "wpm": r["wpm"], "acc": r["acc"]} for r in rows]


# ---- Backward-compatible aliases (pre-boards API shape) ---------------------
# Kept in case anything still calls the old, daily-challenge-only paths.
# New code (and the current frontend) should use /api/score and
# /api/leaderboard[/alltime] with an explicit `board`.


@app.post("/api/daily-score")
def submit_daily_score_legacy(payload: ScoreSubmission):
    payload.board = "daily"
    return submit_score(payload)


@app.get("/api/daily-leaderboard")
def daily_leaderboard_legacy(date: str, script: str = "en", mode: str = "sentences", limit: int = 10):
    return leaderboard(date=date, script=script, mode=mode, board="daily", limit=limit)
