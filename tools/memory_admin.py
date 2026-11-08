"""Verify memory state, checkpoint it, and manage full or incremental recovery."""

import argparse
import json
import secrets
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import audit
import checkpoints
import recovery
from local_files import write_new


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen", help="create a new private 32-byte checkpoint key")
    keygen.add_argument("output", type=Path)
    head = commands.add_parser("head", help="print the verified audit head and store identity as JSON")
    head.add_argument("database", type=Path)
    checkpoint = commands.add_parser("checkpoint", help="save a new HMAC authenticated checkpoint")
    checkpoint.add_argument("database", type=Path)
    checkpoint.add_argument("output", type=Path)
    checkpoint.add_argument("--key-file", type=Path, required=True)
    checkpoint.add_argument("--previous", type=Path, help="require continuity from a protected earlier checkpoint")
    verify = commands.add_parser("verify", help="verify a store's event chain")
    verify.add_argument("database", type=Path)
    verify.add_argument("--expected-head")
    verify.add_argument("--replay", action="store_true", help="compare all facts and embeddings with replay")
    verify.add_argument("--checkpoint", type=Path, help="require the exact externally checkpointed snapshot")
    verify.add_argument("--key-file", type=Path)
    save = commands.add_parser("backup", help="snapshot a running store into a new ZIP file")
    save.add_argument("database", type=Path)
    save.add_argument("archive", type=Path)
    save.add_argument("--key-file", type=Path, help="authenticate the backup head with HMAC")
    save.add_argument("--checkpoint", type=Path, help="require a protected checkpoint as a prefix")
    delta = commands.add_parser("incremental", help="save events after a verified prior backup or checkpoint")
    delta.add_argument("database", type=Path)
    delta.add_argument("archive", type=Path)
    delta.add_argument("--since", type=Path, required=True)
    delta.add_argument("--key-file", type=Path)
    load = commands.add_parser("restore", help="verify and restore a full ZIP and ordered incrementals to a new path")
    load.add_argument("archive", type=Path)
    load.add_argument("database", type=Path)
    load.add_argument("--incremental", type=Path, action="append", default=[])
    load.add_argument("--expected-head")
    load.add_argument("--key-file", type=Path)
    retention = commands.add_parser("retention", help="print a retention plan without deleting archives")
    retention.add_argument("directory", type=Path)
    retention.add_argument("--keep-full", type=int, default=3)
    retention.add_argument("--key-file", type=Path)
    token = commands.add_parser("token", help="generate a bearer token and its hashed configuration entry")
    token.add_argument("--scope", choices=["read", "read-write"], default="read-write")
    args = parser.parse_args()
    try:
        key = checkpoints.key_bytes(args.key_file) if getattr(args, "key_file", None) else None
        if args.command == "keygen":
            checkpoints.create_key(args.output)
            print("created checkpoint key")
            return 0
        if args.command == "token":
            import hashlib
            secret = secrets.token_urlsafe(32)
            scopes = ["read"] if args.scope == "read" else ["read", "write"]
            print(json.dumps({"token": secret, "credential": {
                "token_sha256": hashlib.sha256(secret.encode("ascii")).hexdigest(), "scopes": scopes,
            }}, indent=2))
            return 0
        if args.command == "retention":
            print(json.dumps(recovery.retention_plan(args.directory, args.keep_full, key), indent=2))
            return 0
        if args.command in ("head", "checkpoint", "verify"):
            with closing(recovery.read_only(args.database)) as db:
                db.execute("BEGIN")
                if args.command == "head":
                    print(audit.canonical(checkpoints.state(db)))
                    return 0
                if args.command == "checkpoint":
                    state = checkpoints.state(db)
                    if args.previous:
                        checkpoints.check(db, checkpoints.read(args.previous, key), prefix=True)
                    write_new(args.output, audit.canonical(checkpoints.sign(state, key)).encode("ascii"))
                    count, head = state["count"], state["head"]
                else:
                    count, head = audit.verify(db, args.expected_head, args.replay or bool(args.checkpoint))
                    if args.checkpoint:
                        if key is None:
                            raise ValueError("--checkpoint requires --key-file")
                        checkpoints.check(db, checkpoints.read(args.checkpoint, key))
        elif args.command == "backup":
            if args.checkpoint and key is None:
                raise ValueError("--checkpoint requires --key-file")
            previous = checkpoints.read(args.checkpoint, key) if args.checkpoint else None
            count, head = recovery.backup(args.database, args.archive, key, previous)
        elif args.command == "incremental":
            count, head = recovery.incremental(args.database, args.archive, args.since, key)
        else:
            count, head = recovery.restore(args.archive, args.database, args.incremental, args.expected_head, key)
    except (OSError, ValueError, sqlite3.Error, BadZipFile, KeyError, TypeError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1
    print(f"{args.command} OK | events={count} | head={head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
