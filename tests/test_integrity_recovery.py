import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from zipfile import ZipFile

import pytest
from conftest import ROOT
from test_memory_admin import _admin

sys.path.insert(0, str(ROOT / "server"))
import audit

SERVER = ROOT / "server" / "memory_server.py"


@pytest.fixture
def store(make_client, tmp_path):
    path = tmp_path / "memory.db"
    client = make_client([sys.executable, str(SERVER), str(path)], env={"MCP_EMBEDDINGS": "off"})
    client.initialize()
    assert not client.call_tool("memory_set", {"key": "k", "value": "one"}).get("isError")
    return path, client


def _ok(*args):
    result = _admin(*args)
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("column", ["value", "source_client", "updated_at"])
def test_replay_detects_fact_edits(store, column):
    path, _ = store
    _ok("verify", path, "--replay")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"UPDATE facts SET {column}='tampered' WHERE key='k'")
    _ok("verify", path)
    assert "facts differ" in _admin("verify", path, "--replay").stderr


def test_checkpoint_detects_recomputed_chain_and_truncation(store, tmp_path):
    path, client = store
    key, checkpoint = tmp_path / "key", tmp_path / "checkpoint.json"
    _ok("keygen", key)
    _ok("checkpoint", path, checkpoint, "--key-file", key)
    _ok("verify", path, "--checkpoint", checkpoint, "--key-file", key)
    assert _admin("keygen", key).returncode == 1
    assert _admin("checkpoint", path, checkpoint, "--key-file", key).returncode == 1
    other_key = tmp_path / "other-key"
    _ok("keygen", other_key)
    assert "HMAC mismatch" in _admin("verify", path, "--checkpoint", checkpoint, "--key-file", other_key).stderr
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE facts SET value='forged' WHERE key='k'")
        db.execute("UPDATE events SET value='forged' WHERE action='set'")
        previous = audit.GENESIS
        for row in db.execute(f"SELECT {audit.RECORD_FIELDS} FROM events ORDER BY id"):
            digest = audit._digest(previous, tuple(row[:6]), row[8], row[9])
            db.execute("UPDATE events SET prev_hash=?, event_hash=? WHERE id=?", (previous, digest, row[0]))
            previous = digest
    _ok("verify", path, "--replay")
    assert "does not match" in _admin("verify", path, "--checkpoint", checkpoint, "--key-file", key).stderr
    refused = tmp_path / "refused.json"
    assert "prefix mismatch" in _admin("checkpoint", path, refused, "--key-file", key, "--previous", checkpoint).stderr
    assert not refused.exists()
    assert "prefix mismatch" in _admin("backup", path, tmp_path / "refused.zip",
                                     "--key-file", key, "--checkpoint", checkpoint).stderr
    with closing(sqlite3.connect(path)) as db:
        earlier_fact = db.execute(f"SELECT {audit.FACT_FIELDS} FROM facts WHERE key='k'").fetchone()
    assert not client.call_tool("memory_delete", {"key": "k"}).get("isError")
    latest = tmp_path / "latest.json"
    _ok("checkpoint", path, latest, "--key-file", key)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM events WHERE id=(SELECT MAX(id) FROM events)")
        db.execute(f"INSERT INTO facts ({audit.FACT_FIELDS}) VALUES (?, ?, ?, ?)", earlier_fact)
    _ok("verify", path, "--replay")
    assert _admin("verify", path, "--checkpoint", latest, "--key-file", key).returncode == 1


def test_signed_incrementals_reject_gaps_duplicates_and_wrong_heads(store, tmp_path):
    path, client = store
    key = tmp_path / "key"
    _ok("keygen", key)
    full, first, second = [tmp_path / name for name in ("full.zip", "first.zip", "second.zip")]
    _ok("backup", path, full, "--key-file", key)
    checkpoint = tmp_path / "base-checkpoint"
    _ok("checkpoint", path, checkpoint, "--key-file", key)
    assert not client.call_tool("memory_set", {"key": "k", "value": "two"}).get("isError")
    _ok("incremental", path, first, "--since", checkpoint, "--key-file", key)
    assert not client.call_tool("memory_delete", {"key": "k"}).get("isError")
    assert not client.call_tool("memory_set", {"key": "new", "value": "three"}).get("isError")
    _ok("incremental", path, second, "--since", first, "--key-file", key)
    assert "no events" in _admin("incremental", path, tmp_path / "empty.zip", "--since", second, "--key-file", key).stderr
    head = json.loads(_ok("head", path).stdout)["head"]
    restored = tmp_path / "restored.db"
    _ok("restore", full, restored, "--incremental", first, "--incremental", second,
        "--key-file", key, "--expected-head", head)
    _ok("verify", restored, "--replay", "--expected-head", head)
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("SELECT key, value FROM facts").fetchall() == [("new", "three")]
    for deltas in ([second], [first, first], [second, first]):
        rejected = tmp_path / "rejected.db"
        result = _admin("restore", full, rejected, *[item for delta in deltas for item in ("--incremental", delta)],
                        "--key-file", key)
        assert result.returncode == 1
        assert not rejected.exists()
    assert "head mismatch" in _admin("restore", full, tmp_path / "wrong.db", "--key-file", key,
                                   "--expected-head", "0" * 64).stderr
    assert not (tmp_path / "wrong.db").exists()
    corrupt = tmp_path / "corrupt.zip"
    with ZipFile(first) as src, ZipFile(corrupt, "w") as dst:
        dst.writestr("manifest.json", src.read("manifest.json"))
        dst.writestr("events.json", src.read("events.json") + b"changed")
    assert "checksum mismatch" in _admin("restore", full, tmp_path / "corrupt.db", "--incremental", corrupt,
                                       "--key-file", key).stderr


def test_retention_keeps_dependencies(store, tmp_path):
    path, client = store
    _ok("backup", path, tmp_path / "old.zip")
    client.call_tool("memory_set", {"key": "k", "value": "two"})
    _ok("backup", path, tmp_path / "new.zip")
    client.call_tool("memory_set", {"key": "k", "value": "three"})
    _ok("incremental", path, tmp_path / "delta.zip", "--since", tmp_path / "new.zip")
    plan = json.loads(_ok("retention", tmp_path, "--keep-full", "1").stdout)
    assert plan == {"keep": ["delta.zip", "new.zip"], "candidates": ["old.zip"]}
    assert (tmp_path / "old.zip").exists()


@pytest.mark.parametrize("already_chained", [False, True])
def test_legacy_baseline_preserves_state_and_blocks_older_writers(make_client, tmp_path, already_chained):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE facts (key TEXT PRIMARY KEY, value TEXT NOT NULL, source_client TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE fact_embeddings (key TEXT PRIMARY KEY, embedding BLOB NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                client_id TEXT NOT NULL, action TEXT NOT NULL, key TEXT, value TEXT);
            INSERT INTO facts VALUES ('legacy', 'old', 'old-client', 'fact-time');
            INSERT INTO events(timestamp,client_id,action,key,value) VALUES('different-time','old-client','set','legacy','old');
            INSERT INTO fact_embeddings VALUES ('legacy', X'01020304');
        """)
        if already_chained:
            db.execute("ALTER TABLE events ADD COLUMN prev_hash TEXT")
            db.execute("ALTER TABLE events ADD COLUMN event_hash TEXT")
            row = db.execute(f"SELECT {audit.EVENT_FIELDS} FROM events").fetchone()
            db.execute("UPDATE events SET prev_hash=?, event_hash=?", (audit.GENESIS, audit._digest(audit.GENESIS, row)))
    client = make_client([sys.executable, str(SERVER), str(path)], env={"MCP_EMBEDDINGS": "off"})
    client.initialize()
    client.call_tool("memory_list", {})
    _ok("verify", path, "--replay")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT updated_at FROM facts").fetchone()[0] == "fact-time"
        with pytest.raises(sqlite3.IntegrityError, match="version 2"):
            db.execute("INSERT INTO events(timestamp,client_id,action,prev_hash,event_hash) VALUES('t','c','get','x','y')")
    client.call_tool("memory_set", {"key": "legacy", "value": "new"})
    _ok("verify", path, "--replay")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0] == 0


def test_concurrent_initialization_and_writes_preserve_one_chain(make_client, tmp_path):
    path = tmp_path / "shared.db"
    clients = []
    for _ in range(4):
        client = make_client([sys.executable, str(SERVER), str(path)], env={"MCP_EMBEDDINGS": "off"})
        client.initialize()
        clients.append(client)
    barrier = Barrier(len(clients))

    def writer(client):
        barrier.wait()
        for index in range(20):
            result = client.call_tool("memory_set", {"key": "shared", "value": str(index)})
            assert not result.get("isError"), result

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(writer, clients))
    _ok("verify", path, "--replay")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE action='baseline'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM events WHERE action='set'").fetchone()[0] == 80


def test_upgrade_rejects_damaged_version_one_chain(make_client, tmp_path):
    path = tmp_path / "damaged.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                client_id TEXT NOT NULL, action TEXT NOT NULL, key TEXT, value TEXT, prev_hash TEXT, event_hash TEXT);
            INSERT INTO events VALUES(1,'t','old','set','key','value','wrong','wrong');
        """)
    client = make_client([sys.executable, str(SERVER), str(path)], env={"MCP_EMBEDDINGS": "off"})
    client.initialize()
    assert client.call_tool("memory_set", {"key": "new", "value": "value"})["isError"]
    with closing(sqlite3.connect(path)) as db:
        assert "format_version" not in {r[1] for r in db.execute("PRAGMA table_info(events)")}
        assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
