"""Verify the event chain, back up a live store, or restore to a new path."""

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import audit


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _checksum(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _check_database(path: Path) -> tuple[int, str]:
    with closing(_read_only(path)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        return audit.verify(db)


def _publish(staged: Path, destination: Path) -> None:
    staged.chmod(0o600)
    with staged.open("r+b") as file:
        os.fsync(file.fileno())
    # The staging directory is on the destination volume. A hard link
    # publishes the complete file atomically and fails if the name exists.
    os.link(staged, destination)


def backup(source: Path, archive: Path) -> tuple[int, str]:
    with TemporaryDirectory(dir=archive.parent) as temp:
        snapshot = Path(temp) / "memory.db"
        with closing(_read_only(source)) as src, closing(sqlite3.connect(snapshot)) as dst:
            src.backup(dst, pages=256)
            dst.execute("PRAGMA journal_mode=DELETE")
        result = _check_database(snapshot)
        staged = Path(temp) / "backup.zip"
        with ZipFile(staged, "w") as bundle:
            bundle.write(snapshot, "memory.db")
            bundle.writestr("memory.db.sha256", _checksum(snapshot) + "\n")
        _publish(staged, archive)
    return result


def restore(archive: Path, destination: Path) -> tuple[int, str]:
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(f"{destination}{suffix}"):
            raise FileExistsError(f"SQLite sidecar already exists: {destination}{suffix}")
    with TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp) / "memory.db"
        with ZipFile(archive) as bundle:
            if sorted(bundle.namelist()) != ["memory.db", "memory.db.sha256"]:
                raise ValueError("unexpected backup archive contents")
            with bundle.open("memory.db.sha256") as checksum:
                expected = checksum.read(66)
            with bundle.open("memory.db") as src, staged.open("xb") as dst:
                shutil.copyfileobj(src, dst)
        if expected != (_checksum(staged) + "\n").encode("ascii"):
            raise ValueError("backup SHA-256 checksum mismatch")
        result = _check_database(staged)
        _publish(staged, destination)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="verify a store's event chain")
    verify.add_argument("database", type=Path)
    verify.add_argument("--expected-head", help="compare against a trusted head for this snapshot")
    save = commands.add_parser("backup", help="snapshot a running store into a new ZIP file")
    save.add_argument("database", type=Path)
    save.add_argument("archive", type=Path)
    load = commands.add_parser("restore", help="verify and restore a ZIP file to a new database path")
    load.add_argument("archive", type=Path)
    load.add_argument("database", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            with closing(_read_only(args.database)) as db:
                db.execute("BEGIN")
                count, head = audit.verify(db, args.expected_head)
        elif args.command == "backup":
            count, head = backup(args.database, args.archive)
        else:
            count, head = restore(args.archive, args.database)
    except (OSError, ValueError, sqlite3.Error, BadZipFile) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1
    print(f"{args.command} OK | events={count} | head={head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
