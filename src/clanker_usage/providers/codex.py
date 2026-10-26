"""Codex rate limits, read through Codex's own app-server, with a direct request as the fallback."""

import base64
import hashlib
import json
import math
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .. import cache as files, http, paths

NAME = "codex"
LABEL = "Codex"
PACKAGE = "codex-cli-usage"
DESCRIPTION = "Read Codex rate limits through Codex's own app-server, which owns its login."
USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
# The usage endpoint sits behind a browser check, so the direct request looks like a browser.
BROWSER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
INTERVAL = 300
APP_SERVER_TIMEOUT = 20
KEEP = frozenset({"source", "planType", "plan_type", "limitId", "limitName", "limit_name", "windowDurationMins",
                  "limit_window_seconds", "has_credits", "unlimited"})
WINDOWS = (("5-hour", 5 * 3600), ("daily", 86400), ("weekly", 7 * 86400), ("monthly", 30 * 86400), ("annual", 365 * 86400))


def auth_file():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"


def read_auth():
    auth = files.read_json(auth_file())
    tokens = auth.get("tokens") if auth else None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise RuntimeError(f"Codex is not signed in: no tokens in {auth_file()}; run codex login")
    return tokens


def account():
    """A one-way fingerprint of Codex's ChatGPT account id."""
    account_id = read_auth().get("account_id")
    if not isinstance(account_id, str) or not account_id:
        raise RuntimeError(f"No ChatGPT account id in {auth_file()}")
    return hashlib.sha256(account_id.encode()).hexdigest()[:16]


def jwt_plan(tokens):
    """The plan named in Codex's id token, for responses that leave it out."""
    try:
        claims = json.loads(base64.urlsafe_b64decode(tokens["id_token"].split(".")[1] + "=="))
        return claims["https://api.openai.com/auth"]["chatgpt_plan_type"]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None


def _client_version():
    try:
        return version("clanker-usage")
    except PackageNotFoundError:
        return "0"


def _read_lines(stream, messages):
    try:
        for line in stream:
            messages.put(line)
    finally:
        messages.put(None)


def _response(messages, request_id, deadline):
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is None:
            return None
        try:
            message = json.loads(line)
        except ValueError:
            return None
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    return None


def via_app_server():
    """account/rateLimits/read from `codex app-server`, or None when Codex is missing or does not answer."""
    codex = paths.executable("codex")
    if not codex:
        return None
    deadline = time.monotonic() + APP_SERVER_TIMEOUT
    try:
        process = subprocess.Popen([codex, "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
    except OSError:
        return None
    try:
        messages = queue.Queue()
        threading.Thread(target=_read_lines, args=(process.stdout, messages), daemon=True).start()
        info = {"name": "clanker_usage", "title": "clanker-usage", "version": _client_version()}
        process.stdin.write(json.dumps({"method": "initialize", "id": 1, "params": {"clientInfo": info}}) + "\n")
        process.stdin.flush()
        reply = _response(messages, 1, deadline)
        if not reply or reply.get("error") or "result" not in reply:
            return None
        process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        process.stdin.write(json.dumps({"method": "account/rateLimits/read", "id": 2}) + "\n")
        process.stdin.flush()
        reply = _response(messages, 2, deadline)
        if not reply or reply.get("error") or not isinstance(reply.get("result"), dict):
            return None
        return reply["result"]
    except (OSError, ValueError):
        return None
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def direct():
    """The usage API with the token Codex saved, reread once if Codex renewed it meanwhile; never renewed here."""
    tokens = read_auth()
    for attempt in range(2):
        headers = {"Authorization": "Bearer " + tokens["access_token"], "User-Agent": BROWSER_AGENT,
                   "chatgpt-account-id": tokens.get("account_id", "")}
        try:
            for retry in range(3):
                try:
                    return http.request_json(USAGE_URL, headers)
                except http.StatusError as exc:
                    # A 403 from the browser check is usually transient.
                    if exc.code != 403 or retry == 2:
                        raise
                    time.sleep(0.5 * (retry + 1))
        except http.StatusError as exc:
            latest = read_auth()
            if exc.code not in (401, 403) or attempt or latest.get("access_token") == tokens["access_token"]:
                raise
            tokens = latest


APP_SERVER_KEYS = ("rateLimits", "planType")
API_KEYS = ("plan_type", "rate_limit", "additional_rate_limits", "code_review_rate_limit", "credits")


def fetch():
    """The rate limits from the app-server, else from the API, keeping only what normalize reads.

    Limits keyed by id become a list, so a new limit id is not a new response shape, and the
    account's ids and email are left out.
    """
    plan = jwt_plan(read_auth())
    result = via_app_server()
    if result is not None:
        kept = {key: result[key] for key in APP_SERVER_KEYS if key in result}
        by_id = result.get("rateLimitsByLimitId")
        if isinstance(by_id, dict):
            kept["rateLimitsByLimitId"] = [{"limitId": key, **value} if isinstance(value, dict) else value
                                           for key, value in by_id.items()]
        return {"source": "app-server", "result": kept, "plan_type": plan}
    usage = direct()
    if not isinstance(usage, dict):
        raise ValueError("Codex usage API returned an unexpected response")
    return {"source": "api", "usage": {key: usage[key] for key in API_KEYS if key in usage}, "plan_type": plan}


def number(value, field):
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid Codex rate limit: {field} must be a nonnegative number")
    return value


def window_name(seconds, kind):
    for name, expected in WINDOWS:
        if seconds and abs(seconds - expected) / expected <= 0.1:
            return name
    return kind


def window(kind, used, seconds, reset, scope=None):
    seconds = int(number(seconds, "window length")) if seconds is not None else None
    if seconds is not None and seconds <= 0:
        raise ValueError("Invalid Codex rate limit: window length must be positive")
    return {"name": window_name(seconds, kind), "seconds": seconds, "scope": scope,
            "used_pct": number(used, "used percent"),
            "resets_at": datetime.fromtimestamp(number(reset, "reset time"), timezone.utc).isoformat() if reset is not None else None}


def _app_server_windows(snapshot, scope=None):
    return [window(kind, item.get("usedPercent"), item["windowDurationMins"] * 60 if item.get("windowDurationMins") is not None else None,
                   item.get("resetsAt"), scope)
            for kind in ("primary", "secondary") if isinstance(item := snapshot.get(kind), dict)]


def _api_windows(limits, scope=None):
    return [window(kind, item.get("used_percent"), item.get("limit_window_seconds"), item.get("reset_at"), scope)
            for kind in ("primary", "secondary") if isinstance(item := (limits or {}).get(f"{kind}_window"), dict)]


def normalize(payload):
    """Codex's own limits as unscoped windows, and each additional limit's windows scoped to its name."""
    if payload.get("source") == "app-server":
        result = payload.get("result") or {}
        aggregate = result.get("rateLimits")
        if not isinstance(aggregate, dict):
            raise ValueError("Codex app-server returned no rate limits")
        windows = _app_server_windows(aggregate)
        aggregate_id = aggregate.get("limitId") or "codex"
        for snapshot in result.get("rateLimitsByLimitId") or []:
            limit_id = snapshot.get("limitId") if isinstance(snapshot, dict) else None
            if limit_id in (aggregate_id, "codex", None) or snapshot == aggregate:
                continue
            windows += _app_server_windows(snapshot, snapshot.get("limitName") or limit_id)
        details = {"source": "app-server"}
        if (credits := aggregate.get("credits")) and credits.get("hasCredits"):
            details["credits"] = credits
        plan = result.get("planType") or aggregate.get("planType") or payload.get("plan_type")
        return {"plan": plan, "windows": windows, "details": details}
    usage = payload.get("usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("rate_limit"), dict):
        raise ValueError("Codex usage API returned no rate limits")
    windows = _api_windows(usage["rate_limit"])
    for item in usage.get("additional_rate_limits") or []:
        windows += _api_windows(item.get("rate_limit"), item.get("limit_name"))
    windows += [{**entry, "name": "code review", "scope": "code review"}
                for entry in _api_windows(usage.get("code_review_rate_limit"))[:1]]
    details = {"source": "api"}
    if (credits := usage.get("credits")) and credits.get("has_credits"):
        details["credits"] = credits
    return {"plan": usage.get("plan_type") or payload.get("plan_type"), "windows": windows, "details": details}
