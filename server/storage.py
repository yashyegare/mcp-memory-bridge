"""SQLite schema and connection initialization."""

import sqlite3

import audit


def initialize(conn: sqlite3.Connection, synchronous: str = "NORMAL") -> None:
    if synchronous not in ("NORMAL", "FULL"):
        raise ValueError("MCP_SQLITE_SYNCHRONOUS must be NORMAL or FULL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")      # readers don't block the writer
    conn.execute(f"PRAGMA synchronous={synchronous}")
    conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS facts (
                key           TEXT PRIMARY KEY,
                value         TEXT NOT NULL,
                source_client TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                client_id TEXT NOT NULL,
                action    TEXT NOT NULL,   -- 'set' | 'get' | 'delete' | 'search'
                key       TEXT,
                value     TEXT
            );

            CREATE TABLE IF NOT EXISTS fact_embeddings (
                key       TEXT PRIMARY KEY,   -- mirrors facts.key
                embedding BLOB NOT NULL       -- normalized float32 vector (384 dims)
            );
            """
    )
    audit.migrate(conn)
