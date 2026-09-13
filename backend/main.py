"""
Clackpad leaderboard backend.

A tiny FastAPI service that stores best typing scores for Clackpad's four
online-leaderboard features:

  - "daily"     the daily challenge (one official attempt per person/day)
  - "speedtest" the speed test tab (unlimited practice)
  - "ghost"     ghost race mode
  - "duel"      best WPM achieved in a live 1-v-1 duel (see below)

For each (name, script, mode, board, duration) combination we track two things:
  - the best score *for a given date* (a "daily" board, reset each day)
  - the best score *of all time*      (an "all-time" high-score board)

`duration` only matters for the speed test board (15/30/60/120 seconds) —
daily challenge and ghost race submissions always send duration=0, since a
15-second sprint and a 2-minute run aren't a fair comparison, but daily
challenge/ghost race don't have that axis at all.

It also hosts Duel mode: live 1-v-1 races over a WebSocket
(/ws/duel/{room_code}), matched via a short room code from
POST /api/duel/create, plus a live Lobby (/ws/lobby) that lets people
already on the site see who else is online right now and send/receive
duel invites, instead of only being able to join via a manually-shared
code. Both the lobby and duel rooms are in-memory only, not stored in
the database — see the "Duel mode" / "Lobby" sections below for how
that works.

Everything else in Clackpad (streaks, lessons, badges, the ghost you race
against) still lives in each browser's localStorage — the persistent (DB)
part of this API only ever sees a name + wpm + accuracy, submitted once a
run finishes.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload

Deploy: see ../DEPLOY.md
"""

import os
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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

BOARDS = ("daily", "speedtest", "ghost", "duel")
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


@app.get("/api/leaderboard/combined")
def combined_leaderboard(script: str = "en", scope: str = "today", date: str = "", limit: int = 50):
    """Every board/mode/duration merged into one list, sorted by wpm, each
    row tagged with which board/mode/duration it came from — for the
    Leaderboard tab's "all" view where you don't pick a specific game mode."""
    _check_script(script)
    if scope not in ("today", "alltime"):
        raise HTTPException(400, "scope must be 'today' or 'alltime'")
    limit = max(1, min(limit, 100))
    with get_db() as conn:
        if scope == "alltime":
            rows = conn.execute(
                """
                SELECT name, wpm, acc, board, mode, duration FROM alltime_scores
                WHERE script = ?
                ORDER BY wpm DESC
                LIMIT ?
                """,
                (script, limit),
            ).fetchall()
        else:
            if not date:
                raise HTTPException(400, "date is required when scope='today'")
            rows = conn.execute(
                """
                SELECT name, wpm, acc, board, mode, duration FROM daily_scores
                WHERE script = ? AND date = ?
                ORDER BY wpm DESC
                LIMIT ?
                """,
                (script, date, limit),
            ).fetchall()
    return [
        {
            "name": r["name"], "wpm": r["wpm"], "acc": r["acc"],
            "board": r["board"], "mode": r["mode"], "duration": r["duration"],
        }
        for r in rows
    ]


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


# ---- Duel mode: live 1-v-1 races ------------------------------------------
# Rooms are plain in-memory state, not database rows — a duel only needs to
# exist for the few minutes two people are actually racing each other, so
# there's nothing worth persisting past that (and it conveniently clears
# itself on every redeploy, which is fine for something this short-lived).
# Two players connect to the same room over a WebSocket; once both are in,
# the server picks one shared paragraph and a synchronized start time (a few
# seconds out) so both clients count down to the exact same instant rather
# than racing on whichever one's network happened to respond first. From
# there it's just a relay: each player's live progress gets forwarded to
# the other, and so does their final result.

DUEL_TEXTS = {
    "en": [
        "The quick fox jumped over a lazy dog near the old wooden fence.",
        "A gentle breeze moved through the tall grass as the sun began to set.",
        "She opened the door slowly, unsure of what she might find inside.",
        "Practice a little every day and the habit builds itself over time.",
        "The mountain trail wound upward through pine trees and loose gravel.",
        "Good coffee and a quiet morning make for a productive start to the day.",
    ],
    "ne": [
        "\u0906\u091c \u092e\u094c\u0938\u092e \u0930\u093e\u092e\u094d\u0930\u094b \u091b\u0964 \u0939\u093e\u092e\u0940 \u092c\u093e\u0939\u093f\u0930 \u0918\u0941\u092e\u094d\u0928 \u091c\u093e\u0928\u0947 \u092f\u094b\u091c\u0928\u093e \u092c\u0928\u093e\u092f\u094c\u0964",
        "\u0909\u0938\u0932\u0947 \u0916\u0941\u0938\u0940 \u092d\u090f\u0930 \u092a\u0941\u0930\u093e\u0928\u094b \u0915\u093f\u0924\u093e\u092c \u092a\u0922\u094d\u0928 \u0925\u093e\u0932\u094d\u092f\u094b\u0964",
        "\u092c\u093f\u0939\u093e\u0928 \u092d\u090f\u0915\u094b \u0938\u092e\u092f\u092e\u093e \u092a\u0928\u093f \u0905\u092d\u094d\u092f\u093e\u0938 \u0917\u0930\u094d\u0928\u0941 \u091c\u0930\u0941\u0930\u0940 \u091b\u0964",
        "\u092e\u0932\u093e\u0908 \u0906\u092b\u094d\u0928\u094b \u0918\u0930\u092e\u093e \u092c\u0938\u094d\u0928 \u092e\u0928 \u092a\u0930\u094d\u091b\u0964",
    ],
}


def _gen_room_code() -> str:
    # Skip visually-ambiguous characters (0/O, 1/I/L) since this gets read
    # off one screen and typed into another.
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(4))


class DuelPlayer:
    def __init__(self, ws: WebSocket, name: str):
        self.ws = ws
        self.name = name
        self.connected = True
        self.finished = False
        self.final_wpm = 0
        self.final_acc = 0


class DuelRoom:
    def __init__(self, code: str, script: str):
        self.code = code
        self.script = script
        self.players: List[DuelPlayer] = []
        self.text: Optional[str] = None
        self.start_at: Optional[float] = None
        self.created_at = time.time()

    def other(self, player: DuelPlayer) -> Optional[DuelPlayer]:
        for p in self.players:
            if p is not player:
                return p
        return None

    def is_stale(self) -> bool:
        nobody_connected = not any(p.connected for p in self.players)
        return nobody_connected and (time.time() - self.created_at > 3600)


duel_rooms: Dict[str, DuelRoom] = {}


def _cleanup_stale_duel_rooms() -> None:
    stale = [code for code, room in duel_rooms.items() if room.is_stale()]
    for code in stale:
        duel_rooms.pop(code, None)


@app.post("/api/duel/create")
def create_duel(script: str = "en"):
    _check_script(script)
    _cleanup_stale_duel_rooms()
    code = _gen_room_code()
    while code in duel_rooms:
        code = _gen_room_code()
    duel_rooms[code] = DuelRoom(code, script)
    return {"room": code}


@app.websocket("/ws/duel/{room_code}")
async def duel_socket(websocket: WebSocket, room_code: str):
    room_code = room_code.upper()
    room = duel_rooms.get(room_code)

    if room is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Room not found — check the code, or create a new one."})
        await websocket.close()
        return

    if len([p for p in room.players if p.connected]) >= 2:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "That room already has two players."})
        await websocket.close()
        return

    await websocket.accept()

    try:
        first = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return
    name = (first.get("name") or "player").strip()[:20] or "player"

    player = DuelPlayer(websocket, name)
    room.players.append(player)
    opponent = room.other(player)

    if opponent is None:
        await websocket.send_json({"type": "waiting"})
    else:
        # Room just became full: pick the shared text and a start time a
        # few seconds out, and tell both players at once.
        room.text = random.choice(DUEL_TEXTS[room.script])
        room.start_at = (time.time() + 4.0) * 1000  # ms epoch, comparable to JS Date.now()
        try:
            await websocket.send_json({
                "type": "start", "text": room.text, "startAt": room.start_at,
                "opponentName": opponent.name,
            })
            await opponent.ws.send_json({
                "type": "start", "text": room.text, "startAt": room.start_at,
                "opponentName": player.name,
            })
        except Exception:
            pass

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            opponent = room.other(player)

            if mtype == "progress":
                if opponent and opponent.connected:
                    try:
                        await opponent.ws.send_json({
                            "type": "opponent_progress",
                            "percent": msg.get("percent", 0),
                            "index": msg.get("index", 0),
                            "wpm": msg.get("wpm", 0),
                        })
                    except Exception:
                        pass
            elif mtype == "finished":
                player.finished = True
                player.final_wpm = msg.get("wpm", 0)
                player.final_acc = msg.get("acc", 0)
                if opponent and opponent.connected:
                    try:
                        await opponent.ws.send_json({
                            "type": "opponent_finished",
                            "wpm": player.final_wpm,
                            "acc": player.final_acc,
                        })
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        player.connected = False
        opponent = room.other(player)
        if opponent and opponent.connected:
            try:
                await opponent.ws.send_json({"type": "opponent_left"})
            except Exception:
                pass
        if not any(p.connected for p in room.players):
            duel_rooms.pop(room_code, None)


# ---- Lobby: see who's online right now, invite them to a duel -------------
# A second, separate WebSocket from the duel-room one above. Anyone sitting
# on the Duel screen connects here and shows up in everyone else's online
# list; from there they can send an invite, which — if accepted — creates a
# normal duel room (same DuelRoom/duel_rooms machinery already used by the
# room-code flow) and tells both browsers to connect to it. Nothing here is
# persisted: closing the tab (or navigating away from Duel mode) removes you
# from the list immediately, same as the duel rooms themselves.

class LobbyPlayer:
    def __init__(self, player_id: str, ws: WebSocket, name: str, script: str):
        self.id = player_id
        self.ws = ws
        self.name = name
        self.script = script


lobby_players: Dict[str, LobbyPlayer] = {}


async def _broadcast_lobby() -> None:
    # Everyone gets the full online list minus themselves — sent
    # individually since each recipient's list is different.
    for pid, viewer in list(lobby_players.items()):
        others = [{"id": p.id, "name": p.name} for p in lobby_players.values() if p.id != pid]
        try:
            await viewer.ws.send_json({"type": "players", "players": others})
        except Exception:
            pass


@app.websocket("/ws/lobby")
async def lobby_socket(websocket: WebSocket):
    await websocket.accept()

    try:
        first = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    name = (first.get("name") or "player").strip()[:20] or "player"
    script = first.get("script") if first.get("script") in SCRIPTS else "en"

    player_id = uuid.uuid4().hex[:8]
    player = LobbyPlayer(player_id, websocket, name, script)
    lobby_players[player_id] = player

    try:
        await websocket.send_json({"type": "you", "id": player_id})
    except Exception:
        lobby_players.pop(player_id, None)
        return

    await _broadcast_lobby()

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")

            if mtype == "invite":
                target = lobby_players.get(msg.get("toId"))
                if target is None:
                    try:
                        await websocket.send_json({
                            "type": "invite_failed",
                            "reason": "That player just left.",
                        })
                    except Exception:
                        pass
                else:
                    try:
                        await target.ws.send_json({
                            "type": "invite_received", "fromId": player_id, "fromName": name,
                        })
                    except Exception:
                        pass

            elif mtype == "decline":
                inviter = lobby_players.get(msg.get("toId"))
                if inviter is not None:
                    try:
                        await inviter.ws.send_json({"type": "invite_declined", "byName": name})
                    except Exception:
                        pass

            elif mtype == "accept":
                inviter = lobby_players.get(msg.get("fromId"))
                if inviter is None:
                    try:
                        await websocket.send_json({
                            "type": "invite_failed",
                            "reason": "That player already left.",
                        })
                    except Exception:
                        pass
                    continue
                _cleanup_stale_duel_rooms()
                code = _gen_room_code()
                while code in duel_rooms:
                    code = _gen_room_code()
                duel_rooms[code] = DuelRoom(code, inviter.script)
                for socket in (inviter.ws, websocket):
                    try:
                        await socket.send_json({"type": "invite_accepted", "room": code})
                    except Exception:
                        pass

            elif mtype == "chat":
                # Plain text only, relayed as-is between two lobby members —
                # nothing is stored server-side, same as everything else here.
                text = (msg.get("text") or "").strip()[:500]
                if not text:
                    continue
                target = lobby_players.get(msg.get("toId"))
                if target is None:
                    try:
                        await websocket.send_json({
                            "type": "chat_failed",
                            "reason": "That player just left.",
                            "toId": msg.get("toId"),
                        })
                    except Exception:
                        pass
                else:
                    try:
                        await target.ws.send_json({
                            "type": "chat_received", "fromId": player_id, "fromName": name, "text": text,
                        })
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        lobby_players.pop(player_id, None)
        await _broadcast_lobby()
