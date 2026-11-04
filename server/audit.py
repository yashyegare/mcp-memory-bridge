"""Version 1 of the SQLite event hash chain."""

import hashlib
import json
import sqlite3

GENESIS = "0" * 64
EVENT_FIELDS = "id, timestamp, client_id, action, key, value"


def _digest(previous: str, event: tuple) -> str:
    # An array fixes field order and keeps nulls, strings, and integers distinct.
    payload = json.dumps(["mcp-memory-events-v1", previous, *event],
                         ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _has_chain(db: sqlite3.Connection) -> bool:
    return {"prev_hash", "event_hash"} <= {row[1] for row in db.execute("PRAGMA table_info(events)")}


def migrate(db: sqlite3.Connection) -> None:
    """Seal legacy events once, under the same SQLite writer lock as appends."""
    if _has_chain(db):
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        if _has_chain(db):
            return
        db.execute("ALTER TABLE events ADD COLUMN prev_hash TEXT")
        db.execute("ALTER TABLE events ADD COLUMN event_hash TEXT")
        previous = GENESIS
        for row in db.execute(f"SELECT {EVENT_FIELDS} FROM events ORDER BY id"):
            digest = _digest(previous, tuple(row))
            db.execute("UPDATE events SET prev_hash=?, event_hash=? WHERE id=?",
                       (previous, digest, row[0]))
            previous = digest
        # An older server still running against this file must fail its write
        # transaction instead of appending an event outside the chain.
        db.execute("""
            CREATE TRIGGER events_require_hash BEFORE INSERT ON events
            WHEN NEW.prev_hash IS NULL OR NEW.event_hash IS NULL
            BEGIN SELECT RAISE(ABORT, 'event hashes required'); END
        """)


def append(db: sqlite3.Connection, timestamp: str, client_id: str, action: str,
           key: str | None, value: str | None) -> None:
    """Append under the writer lock. The caller owns commit and rollback."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    tail = db.execute("SELECT event_hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
    previous = tail[0] if tail else GENESIS
    sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
    event_id = sequence[0] + 1 if sequence else 1
    event = (event_id, timestamp, client_id, action, key, value)
    db.execute(f"INSERT INTO events ({EVENT_FIELDS}, prev_hash, event_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (*event, previous, _digest(previous, event)))


def verify(db: sqlite3.Connection, expected_head: str | None = None) -> tuple[int, str]:
    """Verify one consistent snapshot and return its event count and head."""
    if not _has_chain(db):
        raise ValueError("audit chain missing; open the database with the upgraded server first")
    previous = GENESIS
    count = 0
    for row in db.execute(f"SELECT {EVENT_FIELDS}, prev_hash, event_hash FROM events ORDER BY id"):
        event = tuple(row[:-2])
        digest = _digest(previous, event)
        if row[-2] != previous or row[-1] != digest:
            raise ValueError(f"audit chain mismatch at event {event[0]}")
        previous = digest
        count += 1
    if expected_head is not None and previous != expected_head:
        raise ValueError(f"audit head mismatch: expected {expected_head}, found {previous}")
    return count, previous
