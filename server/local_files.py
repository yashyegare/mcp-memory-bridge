"""Publish complete private files without replacing existing names."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory


def publish(staged: Path, destination: Path) -> None:
    staged.chmod(0o600)
    with staged.open("r+b") as file:
        os.fsync(file.fileno())
    os.link(staged, destination)
    if os.name != "nt":
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_new(destination: Path, content: bytes) -> None:
    with TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp) / "content"
        staged.write_bytes(content)
        publish(staged, destination)
