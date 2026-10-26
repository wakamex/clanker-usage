"""Muse Code subscription limits, read with the login Muse Code already holds.

Muse reports its usage in only two places: the key call it makes at /login, and an event at the end
of every model call. This provider makes the key call, which uses no quota. Checked on 2026-10-25
with Muse 1.4.12: the call returns the API key already stored, and leaves Muse's login working.
"""

import hashlib
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import cache as files, http

NAME = "muse-code"
LABEL = "Muse Code"
PACKAGE = "muse-code-usage"
DESCRIPTION = "Read Muse Code subscription limits with the login Muse Code already holds."
KEY_URL = "https://api.meta.ai/muse-code/key"
INTERVAL = 300
KEEP = frozenset({"is_subs_active", "is_subs_upgrade_available", "subs_tier_name", "window_duration_mins"})


def auth_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "muse" / "auth.json"


def login():
    """Muse's Meta login, read without changing Muse's file."""
    meta = ((files.read_json(auth_file()) or {}).get("providers") or {}).get("meta")
    if not isinstance(meta, dict) or not meta.get("access_token") or not meta.get("api_key"):
        raise RuntimeError(f"Muse Code is not signed in: no Meta login in {auth_file()}; run muse and /login")
    return meta


def account():
    """A one-way fingerprint of the account's email, else of its API key."""
    meta = login()
    email = meta.get("user_email")
    source = "email:" + email.strip().lower() if isinstance(email, str) and email.strip() else meta["api_key"]
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def fetch():
    """The subscription part of the key response; the key itself, names and payment details are left out."""
    response = http.request_json(KEY_URL, {"Authorization": "Bearer " + login()["access_token"], "x-api-version": "1.0.0"},
                                 {"onboard": False})
    if not isinstance(response, dict):
        raise ValueError("Muse Code key response is not an object")
    return {key: response[key] for key in ("is_subs_active", "subs_tier_name", "is_subs_upgrade_available", "subs_usage")
            if key in response}


def percent(value):
    # Muse reports a little over 100, such as 101, once a window is used up.
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not 0 <= value <= 1000:
        raise ValueError("Invalid Muse Code usage: used_percent must be a percentage")
    return value


def reset(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid Muse Code usage: resets_at must be a Unix time")
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def normalize(payload):
    """The short window and the weekly limit; an account without a subscription has no plan."""
    if not payload.get("is_subs_active"):
        return {"plan": None, "windows": [], "details": {}}
    usage = payload.get("subs_usage")
    if not isinstance(usage, dict):
        raise ValueError("Muse Code key response has no subscription usage")
    windows = []
    window = usage.get("window")
    if isinstance(window, dict):
        minutes = window.get("window_duration_mins")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise ValueError("Invalid Muse Code usage: window_duration_mins must be a positive whole number")
        name = f"{minutes // 60}-hour" if minutes % 60 == 0 else f"{minutes}-minute"
        windows.append({"name": name, "seconds": minutes * 60, "scope": None,
                        "used_pct": percent(window.get("used_percent")), "resets_at": reset(window.get("resets_at"))})
    weekly = usage.get("weekly")
    if isinstance(weekly, dict):
        windows.append({"name": "weekly", "seconds": 7 * 86400, "scope": None,
                        "used_pct": percent(weekly.get("used_percent")), "resets_at": reset(weekly.get("resets_at"))})
    if not windows:
        raise ValueError("Muse Code key response has no usage windows")
    details = {"upgrade_available": bool(payload.get("is_subs_upgrade_available"))}
    # usage-v1 shares run from 0 to 100, so a used-up window reads 100 and details keep what Muse reported.
    over = {window["name"]: window["used_pct"] for window in windows if window["used_pct"] > 100}
    if over:
        details["reported_used_percent"] = over
        for window in windows:
            window["used_pct"] = min(window["used_pct"], 100)
    plan = payload.get("subs_tier_name")
    return {"plan": plan if isinstance(plan, str) and plan else None, "windows": windows, "details": details}
