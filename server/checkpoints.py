"""HMAC authenticated checkpoints for externally stored audit heads."""

import hashlib
import hmac
import json
import secrets
import sqlite3
from pathlib import Path

import audit
from local_files import write_new


def key_bytes(path: Path) -> bytes:
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("checkpoint key must contain exactly 32 random bytes")
    return key


def create_key(path: Path) -> None:
    write_new(path, secrets.token_bytes(32))


def state(db: sqlite3.Connection) -> dict:
    count, head = audit.verify(db, replay=True)
    last = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    return {"version": 1, "store": audit.identity(db), "count": count, "last_id": last, "head": head}


def sign(state: dict, key: bytes | None) -> dict:
    document = dict(state)
    if key is not None:
        message = b"mcp-memory-checkpoint-v1\n" + audit.canonical(state).encode("ascii")
        document["hmac_sha256"] = hmac.new(key, message, hashlib.sha256).hexdigest()
    return document


def validate(document: dict, key: bytes | None) -> dict:
    if not isinstance(document, dict):
        raise ValueError("invalid checkpoint")
    payload = {k: v for k, v in document.items() if k != "hmac_sha256"}
    if set(payload) != {"version", "store", "count", "last_id", "head"} or payload["version"] != 1:
        raise ValueError("invalid checkpoint format")
    if key is not None:
        expected = sign(payload, key)["hmac_sha256"]
        supplied = document.get("hmac_sha256")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise ValueError("checkpoint HMAC mismatch")
    elif "hmac_sha256" in document:
        raise ValueError("checkpoint key required")
    if any(type(payload[k]) is not int or payload[k] < 1 for k in ("count", "last_id")):
        raise ValueError("invalid checkpoint counters")
    for field in ("head", "store"):
        value = payload[field]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid checkpoint digest")
    return payload


def read(path: Path, key: bytes) -> dict:
    return validate(json.loads(path.read_text(encoding="utf-8")), key)


def check(db: sqlite3.Connection, expected: dict, prefix: bool = False) -> None:
    if prefix:
        row = db.execute("SELECT event_hash FROM events WHERE id=?", (expected["last_id"],)).fetchone()
        count = db.execute("SELECT COUNT(*) FROM events WHERE id<=?", (expected["last_id"],)).fetchone()[0]
        if not row or row[0] != expected["head"] or count != expected["count"] or audit.identity(db) != expected["store"]:
            raise ValueError("checkpoint prefix mismatch")
    elif state(db) != expected:
        raise ValueError("checkpoint does not match this database snapshot")
