"""Devin quota and credits, read with the local API key the Devin CLI already holds."""

import hashlib
import math
import os
import re
import shutil
import subprocess
import tomllib
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .. import http

NAME = "devin"
LABEL = "Devin"
PACKAGE = "devin-cli-usage"
DESCRIPTION = "Read Devin quota and credits with the local API key the Devin CLI already holds."
USER_STATUS_PATH = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"
INTERVAL = 300
KEEP = frozenset({"billingStrategy"})
WINDOWS = (("daily", 86400), ("weekly", 7 * 86400))
PLAN_KEYS = ("planName", "billingStrategy", "monthlyPromptCredits", "monthlyFlowCredits", "monthlyFlexCreditPurchaseAmount")
CREDIT_KEYS = ("availablePromptCredits", "usedPromptCredits", "availableFlowCredits", "usedFlowCredits",
               "availableFlexCredits", "usedFlexCredits", "overageBalanceMicros", "acuConsumed", "acuLimit")


def credentials_file():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "devin" / "credentials.toml"


def credentials():
    """The local API key and its server, read without changing Devin's credentials."""
    path = credentials_file()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise RuntimeError(f"Devin credentials not found at {path}") from None
    except (OSError, tomllib.TOMLDecodeError):
        raise RuntimeError(f"Could not read Devin credentials from {path}") from None
    key, server = data.get("windsurf_api_key"), data.get("api_server_url")
    if not isinstance(key, str) or not key:
        raise RuntimeError("Devin credentials do not contain a local API key")
    parsed = urllib.parse.urlsplit(server if isinstance(server, str) else "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Devin credentials do not contain a valid HTTPS API server")
    return key, f"https://{parsed.netloc}{parsed.path.rstrip('/')}"


def account():
    """A one-way fingerprint of the local API key."""
    return hashlib.sha256(credentials()[0].encode()).hexdigest()[:16]


def devin_version():
    executable = shutil.which("devin")
    if not executable:
        return "unknown"
    try:
        result = subprocess.run([executable, "version"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    match = re.search(r"\b(\d+(?:\.\d+)+)\b", result.stdout)
    return match.group(1) if match else "unknown"


def fetch():
    """The plan and quota parts of the user-status response; its model and feature lists are left out."""
    key, server = credentials()
    version = devin_version()
    metadata = {"apiKey": key, "ideName": "devin", "ideVersion": version, "extensionVersion": version, "locale": "en"}
    payload = http.request_json(server + USER_STATUS_PATH, {"Connect-Protocol-Version": "1"}, {"metadata": metadata})
    status = payload.get("userStatus") if isinstance(payload, dict) else None
    plan_status = status.get("planStatus") if isinstance(status, dict) else None
    if not isinstance(plan_status, dict):
        raise ValueError("Devin user-status response did not contain planStatus")
    info = payload.get("planInfo") or plan_status.get("planInfo") or {}
    return {
        "planInfo": {key: info[key] for key in PLAN_KEYS if key in info} if isinstance(info, dict) else {},
        "planStatus": {key: value for key, value in plan_status.items() if key != "planInfo"},
    }


def number(value):
    """A finite number; the API sends some as strings, such as Unix times."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ValueError("Invalid Devin usage response: expected a number") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid Devin usage response: expected a finite number")
    return int(value) if float(value).is_integer() else value


def normalize(payload):
    status = payload.get("planStatus") if isinstance(payload, dict) else None
    if not isinstance(status, dict):
        raise ValueError("Devin user-status response did not contain planStatus")
    windows = []
    for name, seconds in WINDOWS:
        remaining = status.get(f"{name}QuotaRemainingPercent")
        if remaining is None:
            continue
        remaining = number(remaining)
        if not 0 <= remaining <= 100:
            raise ValueError(f"Invalid Devin usage response: {name} quota outside 0 to 100")
        reset = status.get(f"{name}QuotaResetAtUnix")
        windows.append({"name": name, "seconds": seconds, "scope": None, "used_pct": round(100 - remaining, 6),
                        "resets_at": datetime.fromtimestamp(number(reset), timezone.utc).isoformat() if reset else None})
    credits = {key: number(status[key]) for key in CREDIT_KEYS if key in status}
    if not windows and not credits:
        raise ValueError("Devin user-status response did not contain usage data")
    info = payload.get("planInfo") or {}
    plan = info.get("planName")
    details = {"credits": credits, "plan_period": {"start": status.get("planStart"), "end": status.get("planEnd")}}
    details.update({key: info[key] for key in PLAN_KEYS[1:] if key in info})
    return {"plan": plan if isinstance(plan, str) and plan else None, "windows": windows, "details": details}
