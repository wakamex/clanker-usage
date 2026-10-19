"""OpenCode Go subscription limits, read with the Go API key OpenCode already holds.

The limits come from the OpenCode Console's own /console/api/go/status, which the console's Go
page calls; it is not part of the documented Console API, so its format may change without notice.
"""

import hashlib
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import cache as files, http

NAME = "opencode"
LABEL = "OpenCode Go"
PACKAGE = "opencode-cli-usage"
DESCRIPTION = "Read OpenCode Go subscription limits with the Go API key OpenCode already holds."
STATUS_URL = "https://opencode.ai/console/api/go/status"
INTERVAL = 300
KEEP = frozenset({"product", "renewalProduct", "resumability"})
# The meters, as the console names them; Go's limits are 20%, 50% and 100% of the monthly amount.
METERS = (("fiveHour", "5-hour", 5 * 3600), ("week", "weekly", 7 * 86400), ("month", "monthly", None))
MICRO_CENTS_PER_DOLLAR = 100_000_000


def auth_file():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "auth.json"


def api_key():
    """The key OpenCode stores for its Go provider, read without changing OpenCode's file."""
    auth = files.read_json(auth_file()) or {}
    key = (auth.get("opencode-go") or {}).get("key")
    if not isinstance(key, str) or not key:
        raise RuntimeError(f"OpenCode has no Go key in {auth_file()}; connect OpenCode Go with /connect in OpenCode")
    return key


def account():
    """A one-way fingerprint of the Go key."""
    return hashlib.sha256(api_key().encode()).hexdigest()[:16]


def fetch():
    """The plan, its period and the meters; payment details and user ids are left out."""
    status = http.request_json(STATUS_URL, {"Authorization": "Bearer " + api_key()})
    if not isinstance(status, dict):
        raise ValueError("OpenCode Go status returned an unexpected response")
    access = status.get("access")
    kept = {key: status[key] for key in ("product", "renewalProduct", "resumability", "cancelAtPeriodEnd") if key in status}
    if isinstance(access, dict):
        kept["access"] = {key: access[key] for key in ("startsAt", "endsAt", "cancelAtPeriodEnd", "meters") if key in access}
    elif "access" in status:
        kept["access"] = access
    return kept


def micro_cents(value, field):
    """A nonnegative whole number of micro-cents, which the API sends as a string."""
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError(f"Invalid OpenCode Go meter: {field} must be a whole number")
    return int(value)


def time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("Invalid OpenCode Go meter: malformed reset time") from None
    return parsed.astimezone(timezone.utc).isoformat()


def normalize(payload):
    """One window per meter; a workspace without a Go subscription has no plan and no windows."""
    access = payload.get("access")
    if not access:
        return {"plan": None, "windows": [], "details": {}}
    meters = access.get("meters") if isinstance(access, dict) else None
    if not isinstance(meters, dict):
        raise ValueError("OpenCode Go status has no meters")
    windows, spend = [], {}
    for key, name, seconds in METERS:
        meter = meters.get(key)
        if meter is None:
            continue
        used, limit = micro_cents(meter.get("usedMicroCents"), "used"), micro_cents(meter.get("limitMicroCents"), "limit")
        windows.append({"name": name, "seconds": seconds, "scope": None,
                        "used_pct": used / limit * 100 if limit else 0,
                        "resets_at": time(meter.get("resetsAt")) if meter.get("resetsAt") else None})
        spend[name] = {"used_usd": used / MICRO_CENTS_PER_DOLLAR, "limit_usd": limit / MICRO_CENTS_PER_DOLLAR}
    if not windows:
        raise ValueError("OpenCode Go status has no meters")
    plan = payload.get("product")
    details = {"spend": spend, "renews": payload.get("resumability") == "renewing"}
    return {"plan": plan if isinstance(plan, str) and plan else None, "windows": windows, "details": details}
