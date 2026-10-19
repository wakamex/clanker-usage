"""Z.ai Coding Plan quotas, read with an existing API key."""

import hashlib
import math
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path

from .. import http, paths

NAME = "zcode"
LABEL = "Z.ai"
PACKAGE = "zcode-cli-usage"
DESCRIPTION = "Read Z.ai Coding Plan quotas using an existing API key."
API_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
INTERVAL = 300
# Response keys whose values describe the format rather than the account, kept when recording a shape.
KEEP = frozenset({"code", "success", "msg", "level", "type", "unit", "number"})
# Window length in seconds per API unit; the API uses 3 for hours and 6 for weeks.
UNIT_SECONDS = {3: 3600, 6: 7 * 86400}


def get_api_key(env_file=None):
    key = os.environ.get("ZAI_API_KEY", "").strip()
    if key and env_file is None:
        return key
    source = env_file or paths.config(NAME).get("env_file")
    if source:
        try:
            for line in Path(source).expanduser().read_text().splitlines():
                name, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and name.strip() == "ZAI_API_KEY":
                    parts = shlex.split(value, comments=True)
                    if len(parts) == 1 and parts[0].strip():
                        return parts[0].strip()
                    raise ValueError("Invalid ZAI_API_KEY assignment")
        except (OSError, ValueError):
            raise RuntimeError("Could not read ZAI_API_KEY from the configured env file") from None
    raise RuntimeError("Set ZAI_API_KEY or configure an env file with zcode-cli-usage configure --env-file PATH")


def account():
    """A one-way fingerprint of the API key, since the API names no account."""
    return hashlib.sha256(get_api_key().encode()).hexdigest()[:16]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid quota response: expected a nonnegative finite number")
    return value


def no_plan(payload):
    # An account without a subscription gets a failure whose message names the missing coding plan,
    # for example "当前用户不存在coding plan" ("the current user has no coding plan").
    return (isinstance(payload, dict) and payload.get("success") is False
            and "coding plan" in str(payload.get("msg") or "").lower())


def fetch():
    return http.get_json(API_URL, {"Authorization": "Bearer " + get_api_key(), "Accept": "application/json"})


def window_name(item):
    if item["unit"] == 3:
        return f"{item['number']:g}-hour"
    if item["unit"] == 6 and item["number"] == 1:
        return "weekly"
    return f"{item['type']} (unit {item['unit']:g}, number {item['number']:g})"


def normalize(payload):
    """The plan, windows and details of a quota response; an account without a Coding Plan has no plan."""
    if no_plan(payload):
        return {"plan": None, "windows": [], "details": {"limits": []}}
    if not isinstance(payload, dict) or payload.get("code") != 200 or payload.get("success") is not True:
        raise ValueError("Usage API reported an unsuccessful response")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("limits"), list) or not data["limits"]:
        raise ValueError("Invalid quota response: missing limits")
    windows, limits = [], []
    for item in data["limits"]:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError("Invalid quota response: missing quota type")
        limit = {"type": item["type"], "unit": number(item.get("unit")), "number": number(item.get("number")),
                 "used": number(item.get("currentValue")), "limit": number(item.get("usage")),
                 "remaining": number(item.get("remaining"))}
        reset = item.get("nextResetTime")
        unit_seconds = UNIT_SECONDS.get(limit["unit"])
        windows.append({
            "name": window_name(limit),
            "seconds": int(unit_seconds * limit["number"]) if unit_seconds else None,
            "scope": None,
            # The API's rounded percentage is kept as reported; details hold the exact credit counts.
            "used_pct": number(item.get("percentage")),
            "resets_at": datetime.fromtimestamp(number(reset) / 1000, timezone.utc).isoformat() if reset is not None else None,
        })
        limits.append(limit)
    plan = data.get("level")
    return {"plan": plan if isinstance(plan, str) else None, "windows": windows, "details": {"limits": limits}}


def add_commands(sub):
    configure = sub.add_parser("configure", help="Remember an existing env file that sets ZAI_API_KEY")
    configure.add_argument("--env-file", required=True)


def run_command(args):
    """Run a command that only this provider has; False when args.command is not one."""
    if args.command != "configure":
        return False
    path = str(Path(args.env_file).expanduser().resolve())
    get_api_key(path)
    paths.set_config(NAME, {"env_file": path})
    print(f"Configured {paths.config_file()}")
    return True
