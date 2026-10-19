"""Cursor plan usage, read with the access token Cursor's CLI or IDE already holds."""

import base64
import hashlib
import json
import math
import os
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import http

NAME = "cursor"
LABEL = "Cursor"
PACKAGE = "cursor-cli-usage"
DESCRIPTION = "Read Cursor plan usage with the access token Cursor's CLI or IDE already holds."
API = "https://api2.cursor.sh/aiserver.v1.DashboardService/"
INTERVAL = 300
KEEP = frozenset({"limitType", "includedUsagePeriod", "tier", "enabled"})
HEADERS = {"Connect-Protocol-Version": "1"}


def _config_home():
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def cli_auth_file():
    # Cursor's CLI keeps its tokens here on Linux; other systems use the IDE database below.
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cursor" / "auth.json"


def ide_state_db():
    return _config_home() / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _ide_token():
    path = ide_state_db()
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT value FROM ItemTable WHERE key = ? LIMIT 1",
                                     ("cursorAuth/accessToken",)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        raise RuntimeError(f"Could not read Cursor authentication from {path}") from None
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def access_token():
    """The CLI's access token, else the IDE's, read without refreshing or changing either."""
    try:
        token = json.loads(cli_auth_file().read_text()).get("accessToken")
    except (OSError, ValueError, AttributeError):
        token = None
    if isinstance(token, str) and token:
        return token
    if token := _ide_token():
        return token
    raise RuntimeError("Cursor is not signed in: no token from the Cursor CLI or IDE")


def account():
    """A one-way fingerprint of the token's subject, which stays the same when the token is renewed."""
    try:
        claims = json.loads(base64.urlsafe_b64decode(access_token().split(".")[1] + "=="))
        subject = claims["sub"]
    except (IndexError, KeyError, TypeError, ValueError):
        raise RuntimeError("Cursor access token is not a readable JWT") from None
    return hashlib.sha256(str(subject).encode()).hexdigest()[:16]


def fetch():
    """The usage response and the plan part of the plan-info response."""
    headers = {"Authorization": "Bearer " + access_token(), **HEADERS}
    usage = http.request_json(API + "GetCurrentPeriodUsage", headers, {})
    plan = http.request_json(API + "GetPlanInfo", headers, {})
    return {"usage": usage, "planInfo": plan.get("planInfo") if isinstance(plan, dict) else None}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid Cursor usage response: expected a nonnegative finite number")
    return value


def milliseconds(value):
    """An ISO 8601 UTC time from the API's Unix milliseconds, sent as a string."""
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("Invalid Cursor usage response: expected a time in milliseconds")
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc)


def normalize(payload):
    usage = payload.get("usage") if isinstance(payload, dict) else None
    plan_usage = usage.get("planUsage") if isinstance(usage, dict) else None
    if not isinstance(plan_usage, dict) or "totalPercentUsed" not in plan_usage:
        raise ValueError("Cursor usage response did not contain plan usage")
    start, end = milliseconds(usage.get("billingCycleStart")), milliseconds(usage.get("billingCycleEnd"))
    info = payload.get("planInfo")
    plan = info.get("planName") if isinstance(info, dict) else None
    details = {key: number(plan_usage[key]) for key in
               ("totalSpend", "includedSpend", "limit", "remaining", "autoPercentUsed", "apiPercentUsed")
               if key in plan_usage}
    details["billing_cycle_start"] = start.isoformat()
    spend = usage.get("spendLimitUsage")
    if isinstance(spend, dict) and spend:
        details["spend_limit"] = spend
    return {
        "plan": plan if isinstance(plan, str) and plan else None,
        "windows": [{"name": "billing cycle", "seconds": int((end - start).total_seconds()), "scope": None,
                     "used_pct": number(plan_usage["totalPercentUsed"]), "resets_at": end.isoformat()}],
        "details": details,
    }
