"""Consistent full snapshots and ordered event incrementals."""

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile, is_zipfile

import audit
import checkpoints
from local_files import publish


def read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def checksum(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def check_database(db: sqlite3.Connection, replay: bool = True) -> tuple[int, str]:
    if [tuple(row) for row in db.execute("PRAGMA integrity_check")] != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    return audit.verify(db, replay=replay)


def _manifest(bundle: ZipFile, key: bytes | None) -> dict:
    manifest = json.loads(bundle.read("manifest.json"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("kind") not in ("full", "incremental"):
        raise ValueError("invalid backup manifest")
    manifest["state"] = checkpoints.validate(manifest["state"], key)
    return manifest


def anchor(path: Path, key: bytes | None) -> dict:
    if not is_zipfile(path):
        if key is None:
            raise ValueError("a checkpoint file requires --key-file")
        return checkpoints.read(path, key)
    with ZipFile(path) as bundle:
        return _manifest(bundle, key)["state"]


def backup(source: Path, archive: Path, key: bytes | None = None,
           expected_prefix: dict | None = None) -> tuple[int, str]:
    with TemporaryDirectory(dir=archive.parent) as temp:
        snapshot = Path(temp) / "memory.db"
        with closing(read_only(source)) as src, closing(sqlite3.connect(snapshot)) as dst:
            src.backup(dst, pages=256)
            dst.execute("PRAGMA journal_mode=DELETE")
            check_database(dst)
            state = checkpoints.state(dst)
            if expected_prefix is not None:
                checkpoints.check(dst, expected_prefix, prefix=True)
        manifest = {"version": 1, "kind": "full", "state": checkpoints.sign(state, key)}
        staged = Path(temp) / "backup.zip"
        with ZipFile(staged, "w") as bundle:
            bundle.write(snapshot, "memory.db")
            bundle.writestr("memory.db.sha256", checksum(snapshot) + "\n")
            bundle.writestr("manifest.json", audit.canonical(manifest))
        publish(staged, archive)
    return state["count"], state["head"]


def incremental(source: Path, archive: Path, since: Path, key: bytes | None = None) -> tuple[int, str]:
    previous = anchor(since, key)
    with closing(read_only(source)) as db:
        db.execute("BEGIN")
        state = checkpoints.state(db)
        checkpoints.check(db, previous, prefix=True)
        records = []
        for row in db.execute(f"SELECT {audit.RECORD_FIELDS} FROM events WHERE id>? ORDER BY id",
                              (previous["last_id"],)):
            record = list(row)
            record[-1] = audit.encode_vector(record[-1])
            records.append(record)
    if not records:
        raise ValueError("no events after the previous head")
    payload = audit.canonical(records).encode("ascii")
    manifest = {"version": 1, "kind": "incremental", "from": previous,
                "state": checkpoints.sign(state, key),
                "events_sha256": hashlib.sha256(payload).hexdigest()}
    with TemporaryDirectory(dir=archive.parent) as temp:
        staged = Path(temp) / "incremental.zip"
        with ZipFile(staged, "w") as bundle:
            bundle.writestr("events.json", payload)
            bundle.writestr("manifest.json", audit.canonical(manifest))
        publish(staged, archive)
    return state["count"], state["head"]


def _restore_full(archive: Path, staged: Path, key: bytes | None) -> None:
    with ZipFile(archive) as bundle:
        names = sorted(bundle.namelist())
        legacy = names == ["memory.db", "memory.db.sha256"]
        if not legacy and names != ["manifest.json", "memory.db", "memory.db.sha256"]:
            raise ValueError("unexpected full backup archive contents")
        if legacy and key is not None:
            raise ValueError("legacy backup has no authenticated checkpoint")
        manifest = None if legacy else _manifest(bundle, key)
        if manifest and manifest["kind"] != "full":
            raise ValueError("restore must start with a full backup")
        with bundle.open("memory.db.sha256") as file:
            expected = file.read(66)
        with bundle.open("memory.db") as src, staged.open("xb") as dst:
            shutil.copyfileobj(src, dst)
    if expected != (checksum(staged) + "\n").encode("ascii"):
        raise ValueError("backup SHA-256 checksum mismatch")
    with closing(read_only(staged)) as db:
        db.execute("BEGIN")
        # Preserve recovery of archives produced before the version 2 migration.
        replay = "format_version" in {row[1] for row in db.execute("PRAGMA table_info(events)")}
        check_database(db, replay=replay)
        if manifest:
            checkpoints.check(db, manifest["state"])


def _apply(archive: Path, staged: Path, key: bytes | None) -> None:
    with ZipFile(archive) as bundle:
        if sorted(bundle.namelist()) != ["events.json", "manifest.json"]:
            raise ValueError("unexpected incremental archive contents")
        manifest = _manifest(bundle, key)
        if manifest["kind"] != "incremental":
            raise ValueError("expected incremental archive")
        payload = bundle.read("events.json")
        if hashlib.sha256(payload).hexdigest() != manifest["events_sha256"]:
            raise ValueError("incremental checksum mismatch")
        records = json.loads(payload)
    if not isinstance(records, list) or not records:
        raise ValueError("incremental contains no events")
    with closing(sqlite3.connect(staged)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        checkpoints.check(db, manifest["from"])
        last = manifest["from"]["last_id"]
        head = manifest["from"]["head"]
        for record in records:
            if not isinstance(record, list) or len(record) != 10:
                raise ValueError("invalid incremental event")
            if type(record[0]) is not int or record[0] <= last or record[6] != head or record[8] != 2:
                raise ValueError("incremental event order or predecessor mismatch")
            if record[3] == "baseline":
                raise ValueError("incremental cannot replace the baseline")
            record[-1] = audit.decode_vector(record[-1])
            db.execute(f"INSERT INTO events ({audit.RECORD_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", record)
            last, head = record[0], record[7]
        audit.verify(db)
        facts, vectors = audit.replay_state(db)
        db.execute("DELETE FROM facts")
        db.execute("DELETE FROM fact_embeddings")
        db.executemany(f"INSERT INTO facts ({audit.FACT_FIELDS}) VALUES (?, ?, ?, ?)", facts.values())
        db.executemany("INSERT INTO fact_embeddings (key, embedding) VALUES (?, ?)", vectors.items())
        checkpoints.check(db, manifest["state"])


def restore(archive: Path, destination: Path, incrementals: list[Path] | None = None,
            expected_head: str | None = None, key: bytes | None = None) -> tuple[int, str]:
    if os.path.lexists(destination):
        raise FileExistsError(f"destination already exists: {destination}")
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(f"{destination}{suffix}"):
            raise FileExistsError(f"SQLite sidecar already exists: {destination}{suffix}")
    with TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp) / "memory.db"
        _restore_full(archive, staged, key)
        for delta in incrementals or []:
            _apply(delta, staged, key)
        with closing(read_only(staged)) as db:
            db.execute("BEGIN")
            result = audit.verify(db, expected_head)
        publish(staged, destination)
    return result


def retention_plan(directory: Path, keep_full: int, key: bytes | None = None) -> dict:
    """Keep the newest N full snapshots per store and their incremental descendants."""
    if keep_full < 1:
        raise ValueError("keep_full must be at least 1")
    archives = []
    for path in sorted(directory.glob("*.zip")):
        with ZipFile(path) as bundle:
            manifest = _manifest(bundle, key)
            archives.append((path.name, manifest))
    stores = {m["state"]["store"] for _, m in archives}
    retained = set()
    for store in stores:
        full = sorted(((m["state"]["last_id"], name) for name, m in archives
                       if m["kind"] == "full" and m["state"]["store"] == store), reverse=True)
        retained.update(name for _, name in full[:keep_full])
    reachable = {(m["state"]["store"], m["state"]["head"]) for name, m in archives if name in retained}
    changed = True
    while changed:
        changed = False
        for name, manifest in archives:
            base = manifest.get("from")
            if name not in retained and base and (base["store"], base["head"]) in reachable:
                retained.add(name)
                reachable.add((manifest["state"]["store"], manifest["state"]["head"]))
                changed = True
    return {"keep": sorted(retained), "candidates": sorted(name for name, _ in archives if name not in retained)}
