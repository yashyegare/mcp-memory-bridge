"""Versioned audit records, legacy baselines, and deterministic state replay."""

import base64
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone

GENESIS = "0" * 64
EVENT_FIELDS = "id, timestamp, client_id, action, key, value"
RECORD_FIELDS = EVENT_FIELDS + ", prev_hash, event_hash, format_version, embedding"
FACT_FIELDS = "key, value, source_client, updated_at"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def encode_vector(vector: bytes | None) -> str | None:
    return base64.b64encode(vector).decode("ascii") if vector is not None else None


def decode_vector(vector: str | None) -> bytes | None:
    return base64.b64decode(vector, validate=True) if vector is not None else None


def _digest(previous: str, event: tuple, version: int = 1, embedding: bytes | None = None) -> str:
    payload = [f"mcp-memory-events-v{version}", previous, *event]
    if version == 2:
        payload.append(encode_vector(embedding))
    return hashlib.sha256(canonical(payload).encode("ascii")).hexdigest()


def snapshot(db: sqlite3.Connection) -> dict:
    return {
        "facts": [list(row) for row in db.execute(f"SELECT {FACT_FIELDS} FROM facts ORDER BY key")],
        "embeddings": [[key, encode_vector(vector)] for key, vector in
                       db.execute("SELECT key, embedding FROM fact_embeddings ORDER BY key")],
    }


def migrate(db: sqlite3.Connection) -> None:
    """Seal legacy history and capture its current state once under the writer lock."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
    if "format_version" in columns:
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        if "format_version" in columns:
            return
        if not {"prev_hash", "event_hash"} <= columns:
            db.execute("ALTER TABLE events ADD COLUMN prev_hash TEXT")
            db.execute("ALTER TABLE events ADD COLUMN event_hash TEXT")
            previous = GENESIS
            for row in db.execute(f"SELECT {EVENT_FIELDS} FROM events ORDER BY id"):
                digest = _digest(previous, tuple(row))
                db.execute("UPDATE events SET prev_hash=?, event_hash=? WHERE id=?",
                           (previous, digest, row[0]))
                previous = digest
        else:
            verify(db)
        db.execute("ALTER TABLE events ADD COLUMN format_version INTEGER NOT NULL DEFAULT 1")
        db.execute("ALTER TABLE events ADD COLUMN embedding BLOB")
        append(db, datetime.now(timezone.utc).isoformat(), "server", "baseline", None,
               canonical({"store_nonce": secrets.token_hex(16), **snapshot(db)}))
        db.execute("""
            CREATE TRIGGER events_require_v2 BEFORE INSERT ON events
            WHEN NEW.format_version != 2 OR NEW.prev_hash IS NULL OR NEW.event_hash IS NULL
            BEGIN SELECT RAISE(ABORT, 'version 2 audit record required'); END
        """)


def append(db: sqlite3.Connection, timestamp: str, client_id: str, action: str,
           key: str | None, value: str | None, embedding: bytes | None = None) -> None:
    """The caller owns commit and rollback of fact, vector, and event together."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    tail = db.execute("SELECT event_hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
    previous = tail[0] if tail else GENESIS
    sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
    event_id = sequence[0] + 1 if sequence else 1
    event = (event_id, timestamp, client_id, action, key, value)
    db.execute(f"INSERT INTO events ({RECORD_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (*event, previous, _digest(previous, event, 2, embedding), 2, embedding))


def verify(db: sqlite3.Connection, expected_head: str | None = None,
           replay: bool = False) -> tuple[int, str]:
    """Caller must hold a read snapshot. Verify hashes and optionally all live state."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
    if not {"prev_hash", "event_hash"} <= columns:
        raise ValueError("audit chain missing; open the database with the upgraded server first")
    versioned = "format_version" in columns
    fields = RECORD_FIELDS if versioned else EVENT_FIELDS + ", prev_hash, event_hash"
    previous = GENESIS
    count = 0
    for row in db.execute(f"SELECT {fields} FROM events ORDER BY id"):
        event = tuple(row[:6])
        version, vector = (row[8], row[9]) if versioned else (1, None)
        if version not in (1, 2):
            raise ValueError(f"unknown audit version at event {event[0]}")
        digest = _digest(previous, event, version, vector)
        if row[6] != previous or row[7] != digest:
            raise ValueError(f"audit chain mismatch at event {event[0]}")
        previous = digest
        count += 1
    if expected_head is not None and previous != expected_head:
        raise ValueError(f"audit head mismatch: expected {expected_head}, found {previous}")
    if replay:
        facts, vectors = replay_state(db)
        actual = snapshot(db)
        if actual["facts"] != [list(facts[key]) for key in sorted(facts)]:
            raise ValueError("facts differ from audit replay")
        if actual["embeddings"] != [[key, encode_vector(vectors[key])] for key in sorted(vectors)]:
            raise ValueError("embeddings differ from audit replay")
    return count, previous


def replay_state(db: sqlite3.Connection) -> tuple[dict, dict]:
    """Reconstruct state after the sealed baseline, preserving exact timestamps."""
    baseline = db.execute("SELECT id, value FROM events WHERE action='baseline' AND format_version=2 ORDER BY id").fetchall()
    if len(baseline) != 1:
        raise ValueError("replay requires exactly one sealed baseline; upgrade the database first")
    start, payload = baseline[0]
    state = json.loads(payload)
    if not isinstance(state, dict) or not isinstance(state.get("facts"), list) or not isinstance(state.get("embeddings"), list):
        raise ValueError("invalid audit baseline")
    if any(not isinstance(row, list) or len(row) != 4 or
           any(not isinstance(value, str) for value in row) for row in state["facts"]):
        raise ValueError("invalid fact in audit baseline")
    if any(not isinstance(row, list) or len(row) != 2 or
           any(not isinstance(value, str) for value in row) for row in state["embeddings"]):
        raise ValueError("invalid vector in audit baseline")
    facts = {row[0]: tuple(row) for row in state["facts"]}
    vectors = {key: decode_vector(vector) for key, vector in state["embeddings"]}
    if len(facts) != len(state["facts"]) or len(vectors) != len(state["embeddings"]):
        raise ValueError("duplicate keys in audit baseline")
    for row in db.execute(f"SELECT {RECORD_FIELDS} FROM events WHERE id>=? ORDER BY id", (start,)):
        event_id, timestamp, client, action, key, value, _, _, version, vector = row
        if version != 2:
            raise ValueError(f"non-replayable audit version at event {event_id}")
        if action == "set":
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"invalid set at event {event_id}")
            facts[key] = (key, value, client, timestamp)
            vectors.pop(key, None)
            if vector is not None:
                vectors[key] = vector
        elif action == "delete":
            if not isinstance(key, str):
                raise ValueError(f"invalid delete at event {event_id}")
            facts.pop(key, None)
            vectors.pop(key, None)
        elif action not in ("baseline", "get", "search", "cas_fail", "demo-lock-hold"):
            raise ValueError(f"unknown audit action at event {event_id}")
    return facts, vectors


def identity(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT event_hash FROM events WHERE action='baseline' AND format_version=2").fetchone()
    if row is None:
        raise ValueError("database has no sealed baseline")
    return row[0]
