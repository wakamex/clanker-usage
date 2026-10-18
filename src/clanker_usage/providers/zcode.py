"""Z.ai Coding Plan quotas, read with an existing API key."""

import math
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path

from .. import cache, http

NAME = "zcode"
PACKAGE = "zcode-cli-usage"
DESCRIPTION = "Read Z.ai Coding Plan quotas using an existing API key."
API_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
INTERVAL = 300
# Response keys whose values describe the format rather than the account, kept when recording a shape.
KEEP = frozenset({"code", "success", "msg", "level", "type", "unit", "number"})


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "zcode-cli-usage/config.json"


def cache_path():
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "zcode-cli-usage/usage.json"


def get_api_key(env_file=None):
    key = os.environ.get("ZAI_API_KEY", "").strip()
    if key:
        return key
    source = env_file or os.environ.get("ZCODE_USAGE_ENV_FILE") or (cache.read_json(config_path()) or {}).get("env_file")
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


def normalize(payload):
    if no_plan(payload):
        return {"plan": None, "status": "no_plan", "source": "api",
                "updated_at": datetime.now(timezone.utc).isoformat(), "limits": []}
    if not isinstance(payload, dict) or payload.get("code") != 200 or payload.get("success") is not True:
        raise ValueError("Usage API reported an unsuccessful response")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("limits"), list) or not data["limits"]:
        raise ValueError("Invalid quota response: missing limits")
    limits = []
    for item in data["limits"]:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError("Invalid quota response: missing quota type")
        unit = number(item.get("unit"))
        count = number(item.get("number"))
        reset = item.get("nextResetTime")
        limits.append({
            "type": item["type"], "unit": unit, "number": count,
            "used": number(item.get("currentValue")),
            "limit": number(item.get("usage")),
            "remaining": number(item.get("remaining")),
            "pct": number(item.get("percentage")),
            "resets_at": datetime.fromtimestamp(number(reset) / 1000, timezone.utc).isoformat() if reset is not None else None,
        })
    return {"plan": data.get("level", "unknown"), "source": "api", "updated_at": datetime.now(timezone.utc).isoformat(), "limits": limits}


def label(item):
    if item["unit"] == 3:
        return f"{item['number']:g}-hour"
    if item["unit"] == 6 and item["number"] == 1:
        return "Weekly"
    return f"{item['type']} (unit {item['unit']:g}, number {item['number']:g})"


def windows(record):
    return [{"name": label(item).lower(), "used_pct": item["pct"], "resets_at": item["resets_at"]}
            for item in record["limits"]]


def display(record, compact=False):
    if record.get("status") == "no_plan":
        return "No active Coding Plan"
    parts = []
    for item in record["limits"]:
        suffix = " credits" if item["type"] == "CREDIT_LIMIT" else f" {item['type']}"
        text = f"{label(item)}: {item['pct']:g}% ({item['used']:g}/{item['limit']:g}{suffix})"
        if item["resets_at"]:
            reset = datetime.fromisoformat(item["resets_at"]).astimezone()
            text += f" resets {reset:%Y-%m-%d %H:%M %Z}"
        parts.append(text)
    if record.get("stale"):
        parts.append(f"Cached, live fetch failed; updated {record['updated_at']}")
    return " | ".join(parts) if compact else f"Plan: {record['plan']}\n" + "\n".join(parts)


def add_commands(sub):
    configure = sub.add_parser("configure", help="Remember an existing env file path")
    configure.add_argument("--env-file", required=True)


def run_command(args):
    """Run a command that only this provider has; False when args.command is not one."""
    if args.command != "configure":
        return False
    path = str(Path(args.env_file).expanduser().resolve())
    # Validate the selected file even if a key is present in the environment.
    existing = os.environ.pop("ZAI_API_KEY", None)
    try:
        get_api_key(path)
    finally:
        if existing is not None:
            os.environ["ZAI_API_KEY"] = existing
    cache.write_json(config_path(), {"env_file": path})
    print(f"Configured {config_path()}")
    return True
