import sqlite3
import subprocess
import sys
from contextlib import closing
from zipfile import ZipFile

from conftest import ROOT

SERVER = ROOT / "server" / "memory_server.py"
ADMIN = ROOT / "tools" / "memory_admin.py"


def _admin(*args):
    return subprocess.run([sys.executable, str(ADMIN), *map(str, args)],
                          capture_output=True, text=True, timeout=15, check=False)


def test_legacy_migration_backup_restore_and_verification(make_client, tmp_path):
    db = tmp_path / "memory.db"
    archive = tmp_path / "backup.zip"
    restored = tmp_path / "restored.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                client_id TEXT NOT NULL, action TEXT NOT NULL, key TEXT, value TEXT
            );
            INSERT INTO events VALUES (7, 'legacy-time', 'legacy', 'set', 'old', 'original');
        """)
    live = make_client([sys.executable, str(SERVER), str(db)], env={"MCP_EMBEDDINGS": "off"})
    live.initialize()
    assert not live.call_tool("memory_set", {"key": "snapshot", "value": "before"}).get("isError")
    saved = _admin("backup", db, archive)
    assert saved.returncode == 0, saved.stderr
    assert "events=2" in saved.stdout
    head = saved.stdout.strip().split("head=")[1]
    assert not live.call_tool("memory_set", {"key": "snapshot", "value": "after"}).get("isError")
    recovered = _admin("restore", archive, restored)
    assert recovered.returncode == 0, recovered.stderr
    assert _admin("restore", archive, restored).returncode == 1
    assert _admin("verify", restored, "--expected-head", head).returncode == 0
    reader = make_client([sys.executable, str(SERVER), str(restored)],
                         env={"MCP_EMBEDDINGS": "off", "MCP_LOG_READS": "off"})
    reader.initialize()
    assert "snapshot = before" in reader.call_tool("memory_get", {"key": "snapshot"})["content"][0]["text"]
    assert "legacy" in reader.call_tool("memory_history", {"key": "old"})["content"][0]["text"]
    with closing(sqlite3.connect(restored)) as conn, conn:
        conn.execute("DELETE FROM events WHERE id=8")
    assert _admin("verify", restored).returncode == 0
    assert "head mismatch" in _admin("verify", restored, "--expected-head", head).stderr
    with closing(sqlite3.connect(restored)) as conn, conn:
        conn.execute("UPDATE events SET value='tampered' WHERE id=7")
    assert "mismatch at event 7" in _admin("verify", restored).stderr

    corrupt = tmp_path / "corrupt.zip"
    with ZipFile(archive) as valid, ZipFile(corrupt, "w") as invalid:
        invalid.writestr("memory.db", valid.read("memory.db") + b"changed")
        invalid.writestr("memory.db.sha256", valid.read("memory.db.sha256"))
    rejected = tmp_path / "rejected.db"
    assert "SHA-256 checksum mismatch" in _admin("restore", corrupt, rejected).stderr
    assert not rejected.exists()
