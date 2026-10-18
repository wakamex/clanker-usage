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
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def age(record):
    """Seconds since the record's updated_at, or None when it is missing or malformed."""
    try:
        updated = datetime.fromisoformat(record["updated_at"])
        return (datetime.now(timezone.utc) - updated).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def is_fresh(record, max_age):
    seconds = age(record)
    return seconds is not None and 0 <= seconds < max_age
