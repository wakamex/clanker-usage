"""Devin quota and credits, read with the local API key the Devin CLI already holds."""

import glob
import hashlib
import math
import os
import re
import time
import shutil
import subprocess
import tomllib
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import http, paths

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


# Devin's free models have a rate limit that its status API does not report; the only record is the
# error Devin writes to its CLI log, such as "Reached free model rate limit. ... Your limit will reset
# in 1 minute (at 09:51 UTC)." It names no model, so a hit's model comes from the log's model line.
RATE_LIMIT = re.compile(r"^(\S+)\s.*Reached free model rate limit\..*?reset in [^(]*\(at (\d{1,2}):(\d{2}) UTC\)")
MODEL_LINE = re.compile(r"resolved_model_uid=([A-Za-z0-9_.-]+)|model_uid=([A-Za-z0-9_.-]+)")
# Logs older than this are not read, so status stays fast on hosts with many Devin homes.
LOG_MAX_AGE = 7 * 86400
HISTORY = 50


def data_dir():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "devin"


def log_dirs():
    """Devin's own log directory and every directory matching a glob added with configure --log-glob."""
    found = [data_dir() / "cli" / "logs"]
    for pattern in paths.config(NAME).get("log_globs", []):
        found += [Path(match) for match in sorted(glob.glob(os.path.expanduser(pattern)))]
    return list(dict.fromkeys(folder for folder in found if folder.is_dir()))


def rate_limit_hits(now=None):
    """Each free-model rate-limit hit in recent Devin logs: when, the reset it named, and the model when the log gives it.

    Devin retries a failed request and logs the same error several times, so lines in one log that name
    the same reset count as one hit.
    """
    cutoff = (now or time.time()) - LOG_MAX_AGE
    hits = {}
    for folder in log_dirs():
        for log in folder.glob("*.log"):
            try:
                if log.stat().st_mtime < cutoff:
                    continue
                lines = log.read_text(errors="replace").splitlines()
            except OSError:
                continue
            model = None
            for line in lines:
                if (found := MODEL_LINE.search(line)) and "Reached free model rate limit" not in line:
                    model = found.group(1) or found.group(2)
                if "Reached free model rate limit" not in line or not (match := RATE_LIMIT.match(line)):
                    continue
                try:
                    at = datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).astimezone(timezone.utc)
                except ValueError:
                    continue
                reset = at.replace(hour=int(match.group(2)), minute=int(match.group(3)), second=0, microsecond=0)
                if reset < at - timedelta(minutes=1):
                    reset += timedelta(days=1)  # The reset is after midnight UTC.
                key = (str(log), reset)
                if key not in hits:
                    hits[key] = {"at": at.isoformat(timespec="seconds"), "resets_at": reset.isoformat(), "model": model}
    return sorted(hits.values(), key=lambda hit: hit["at"])


def free_model_limits(hits, now):
    """Per model, the last hit, its reset and whether the limit still applies at now; a model of None is unnamed."""
    latest = {}
    for hit in hits:
        latest[hit["model"]] = hit
    return [{"model": model, "last_hit_at": hit["at"], "resets_at": hit["resets_at"],
             "limited": datetime.fromisoformat(hit["resets_at"]) > now}
            for model, hit in sorted(latest.items(), key=lambda item: item[1]["at"], reverse=True)]


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
        "rateLimitHits": rate_limit_hits()[-HISTORY:],
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
    hits = payload.get("rateLimitHits") or []
    if not isinstance(hits, list) or not all(isinstance(hit, dict) and isinstance(hit.get("resets_at"), str) for hit in hits):
        raise ValueError("Devin rate-limit hits are malformed")
    details["free_model_limits"] = free_model_limits(hits, datetime.now(timezone.utc))
    details["free_model_limit_hits"] = hits
    return {"plan": plan if isinstance(plan, str) and plan else None, "windows": windows, "details": details}


def notes(record):
    """Free models whose rate limit applies now, for status and statusline."""
    now = datetime.now(timezone.utc)
    out = []
    for limit in (record.get("details") or {}).get("free_model_limits") or []:
        reset = datetime.fromisoformat(limit["resets_at"])
        if reset > now:
            out.append(f"{limit['model'] or 'free model'} rate-limited until {reset.astimezone():%H:%M}")
    return out


def add_commands(sub):
    configure = sub.add_parser("configure", help="Also read Devin logs from directories matching a glob")
    configure.add_argument("--log-glob", required=True,
                           help="such as the logs of Devin homes another tool creates, ~/work/*/devin/cli/logs")


def run_command(args):
    if args.command != "configure":
        return False
    section = paths.config(NAME)
    section["log_globs"] = list(dict.fromkeys([*section.get("log_globs", []), args.log_glob]))
    paths.set_config(NAME, section)
    print(f"Configured {paths.config_file()}; {len(log_dirs())} Devin log directories match")
    return True
