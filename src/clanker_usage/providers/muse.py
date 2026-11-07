"""Muse Code subscription limits, read with the login Muse Code already holds.

Muse reports its usage in only two places: the key call it makes at /login, and an event at the end
of every model call. This provider makes the key call, which uses no quota. Checked on 2026-10-25
with Muse 1.4.12: the call returns the API key already stored, and leaves Muse's login working.
"""

import glob
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import cache as files, http, paths

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


# How far back to look for a recorded Muse run, and how much of each run's output to read: the usage
# record is the output's last line.
RUN_MAX_AGE = 7 * 86400
RUN_TAIL = 1 << 16
ALLOWANCE = re.compile(rb'"allowance"\s*:\s*\{"as_of_ms"')


def run_files():
    """The output files of recorded Muse runs, from globs added with configure --run-glob."""
    found = []
    for pattern in paths.config(NAME).get("run_globs", []):
        found += glob.glob(os.path.expanduser(pattern))
    return found


def recorded_allowance():
    """The newest usage that a Muse run recorded, as the `allowance` of `muse exec --json`'s run_usage record.

    Muse sends usage at the end of every model call, and `muse exec --json` ends its output with the
    last of them. Muse saves it nowhere else, but a tool that keeps the output, such as aop in each
    run's events.jsonl, leaves it here to read without a model call.
    """
    cutoff = time.time() - RUN_MAX_AGE
    newest = None
    for name in run_files():
        try:
            if os.path.getmtime(name) < cutoff:
                continue
            with open(name, "rb") as stream:
                stream.seek(max(0, os.path.getsize(name) - RUN_TAIL))
                tail = stream.read()
        except OSError:
            continue
        if not ALLOWANCE.search(tail):
            continue
        for line in reversed(tail.splitlines()):
            if b'"allowance"' not in line:
                continue
            try:
                allowance = _find_allowance(json.loads(line))
            except ValueError:
                continue
            if allowance and (newest is None or allowance["as_of_ms"] > newest["as_of_ms"]):
                newest = allowance
            break
    return newest


def _find_allowance(value):
    if isinstance(value, dict):
        found = value.get("allowance")
        if isinstance(found, dict) and isinstance(found.get("as_of_ms"), int):
            return found
        for item in value.values():
            if found := _find_allowance(item):
                return found
    elif isinstance(value, list):
        for item in value:
            if found := _find_allowance(item):
                return found
    return None


def fetch():
    """The subscription part of the key response, and the newest run-recorded usage when it has none.

    The key itself, names and payment details are left out.
    """
    response = http.request_json(KEY_URL, {"Authorization": "Bearer " + login()["access_token"], "x-api-version": "1.0.0"},
                                 {"onboard": False})
    if not isinstance(response, dict):
        raise ValueError("Muse Code key response is not an object")
    payload = {key: response[key] for key in ("is_subs_active", "subs_tier_name", "is_subs_upgrade_available", "subs_usage")
               if key in response}
    if "subs_usage" not in payload and (allowance := recorded_allowance()):
        payload["run_allowance"] = allowance
    return payload


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
    retrieved_at = None
    if usage is None and isinstance(payload.get("run_allowance"), dict):
        usage, retrieved_at = _usage_from_allowance(payload["run_allowance"])
    if not isinstance(usage, dict):
        raise ValueError("Muse Code's key response did not include usage, and no recorded Muse run has it")
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
    body = {"plan": plan if isinstance(plan, str) and plan else None, "windows": windows, "details": details}
    if retrieved_at:
        details["source"] = "recorded run"
        body["retrieved_at"] = retrieved_at
    return body


def _usage_from_allowance(allowance):
    """The key response's usage shape from a run's allowance, and when Muse observed it.

    The allowance gives reset times in milliseconds and leaves out the short window's length, which
    every observed usage event gives as 300 minutes.
    """
    as_of = allowance.get("as_of_ms")
    if isinstance(as_of, bool) or not isinstance(as_of, int) or as_of <= 0:
        raise ValueError("Invalid Muse Code run allowance: as_of_ms must be a time in milliseconds")
    usage = {}
    for key in ("window", "weekly"):
        part = allowance.get(key)
        if not isinstance(part, dict):
            continue
        resets = part.get("resets_at_ms")
        if resets is not None and (isinstance(resets, bool) or not isinstance(resets, int | float)):
            raise ValueError("Invalid Muse Code run allowance: resets_at_ms must be a time in milliseconds")
        usage[key] = {"used_percent": part.get("used_percent"), "resets_at": resets / 1000 if resets is not None else None}
    if "window" in usage:
        usage["window"]["window_duration_mins"] = 300
    return usage, datetime.fromtimestamp(as_of / 1000, timezone.utc).isoformat(timespec="seconds")


def add_commands(sub):
    configure = sub.add_parser("configure", help="Also read Muse usage that tools such as aop recorded from muse exec --json")
    configure.add_argument("--run-glob", required=True,
                           help="output files of recorded Muse runs, such as aop's ~/work/*/.aop/runs/*/events.jsonl")


def run_command(args):
    if args.command != "configure":
        return False
    section = paths.config(NAME)
    section["run_globs"] = list(dict.fromkeys([*section.get("run_globs", []), args.run_glob]))
    paths.set_config(NAME, section)
    print(f"Configured {paths.config_file()}; {len(run_files())} run output files match")
    return True
