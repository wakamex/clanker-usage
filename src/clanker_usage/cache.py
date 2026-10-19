"""Private, atomically written JSON files for caches and check state."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

LIMIT = 1 << 20


def read_json(path):
    """The object stored at path, or None when it is missing, oversized or malformed."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(LIMIT + 1)
        data = json.loads(raw) if len(raw) <= LIMIT else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path, data):
    """Replace path with data through a private temporary file in the same directory."""
    path = Path(path)
    # mkdir(parents=True) gives only the last directory the mode, so create each missing one privately.
    for folder in reversed([path.parent, *path.parent.parents]):
        if not folder.exists():
            # Another writer, such as a parallel provider in the all-accounts view, may create it first.
            folder.mkdir(mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".usage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def age(record, key="retrieved_at"):
    """Seconds since the record's time under key, or None when it is missing or malformed."""
    try:
        updated = datetime.fromisoformat(record[key])
        return (datetime.now(timezone.utc) - updated).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def is_fresh(record, max_age):
    return is_fresh_at(record, "retrieved_at", max_age)


def is_fresh_at(record, key, max_age):
    seconds = age(record, key)
    return seconds is not None and 0 <= seconds < max_age
