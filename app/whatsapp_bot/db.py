"""SQLite persistence: message ledger + key/value state.

The api and the worker are separate processes writing to the same file on a
shared volume, so the connection is opened in WAL mode with a busy timeout and
in autocommit mode. Holding an implicit transaction open across a request would
block the other process for no reason.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from whatsapp_bot.models import InboundMessage, Outbound

Direction = Literal["in", "out"]
InboundStatus = Literal["received", "queued", "processing", "processed", "failed", "dead"]
OutboundStatus = Literal["queued", "sent", "failed", "rate_limited"]

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    direction    TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    chat_jid     TEXT NOT NULL,
    from_jid     TEXT,
    is_group     INTEGER NOT NULL DEFAULT 0,
    type         TEXT,
    text         TEXT,
    quoted_id    TEXT,
    wa_timestamp INTEGER,
    status       TEXT NOT NULL,
    error        TEXT,
    handler      TEXT,
    reply_to     TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT ({_NOW}),
    updated_at   TEXT NOT NULL DEFAULT ({_NOW})
);

CREATE INDEX IF NOT EXISTS idx_messages_direction_status ON messages (direction, status);
CREATE INDEX IF NOT EXISTS idx_messages_created_at      ON messages (created_at);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to        ON messages (reply_to);

CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ({_NOW})
);
"""


def connect(database_path: str) -> sqlite3.Connection:
    """Open a tuned connection. Cheap enough to do once per request or per job."""
    if database_path != ":memory:":
        Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(database_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def session(database_path: str) -> Iterator[sqlite3.Connection]:
    """Context manager yielding an initialised connection."""
    conn = connect(database_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


# --- inbound -----------------------------------------------------------------


def record_inbound(conn: sqlite3.Connection, msg: InboundMessage) -> bool:
    """Insert an inbound message.

    Returns ``True`` when it was new and ``False`` when the id was already
    known. ``INSERT OR IGNORE`` on the primary key makes this the deduplication
    point: there is no window between a SELECT and an INSERT for a duplicate to
    slip through, even with concurrent webhook deliveries.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (id, direction, chat_jid, from_jid, is_group, type, text,
             quoted_id, wa_timestamp, status)
        VALUES (?, 'in', ?, ?, ?, ?, ?, ?, ?, 'received')
        """,
        (
            msg.id,
            msg.chat_jid,
            msg.from_jid,
            int(msg.is_group),
            msg.type,
            msg.text,
            msg.quoted_id,
            msg.timestamp,
        ),
    )
    return cur.rowcount == 1


def mark_status(
    conn: sqlite3.Connection,
    message_id: str,
    status: InboundStatus | OutboundStatus,
    *,
    error: str | None = None,
    handler: str | None = None,
    bump_attempts: bool = False,
) -> None:
    conn.execute(
        f"""
        UPDATE messages
           SET status = ?,
               error = ?,
               handler = COALESCE(?, handler),
               attempts = attempts + ?,
               updated_at = {_NOW}
         WHERE id = ?
        """,  # _NOW is a module constant, never user input
        (status, error, handler, int(bump_attempts), message_id),
    )


def get_message(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
    row: sqlite3.Row | None = cur.fetchone()
    return row


def recent_messages(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM messages ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    )
    return list(cur.fetchall())


# --- outbound ----------------------------------------------------------------


def record_outbound(
    conn: sqlite3.Connection,
    outbound_id: str,
    out: Outbound,
    *,
    status: OutboundStatus,
    reply_to: str | None = None,
    error: str | None = None,
) -> None:
    # A document's ledger line records the filename, so `make db` shows what
    # actually left rather than an empty caption.
    text = out.text
    if out.is_document:
        label = out.filename or str(out.document_path)
        text = f"[{label}] {out.text}".strip()

    conn.execute(
        """
        INSERT OR REPLACE INTO messages
            (id, direction, chat_jid, from_jid, is_group, type, text,
             quoted_id, status, error, handler, reply_to)
        VALUES (?, 'out', ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            outbound_id,
            out.jid,
            int(out.jid.endswith("@g.us")),
            "document" if out.is_document else "text",
            text,
            out.quoted_id,
            status,
            error,
            out.handler,
            reply_to,
        ),
    )


# --- state -------------------------------------------------------------------


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    cur = conn.execute("SELECT value FROM state WHERE key = ?", (key,))
    row = cur.fetchone()
    return str(row["value"]) if row is not None else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        f"""
        INSERT INTO state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = {_NOW}
        """,  # _NOW is a module constant, never user input
        (key, value),
    )
