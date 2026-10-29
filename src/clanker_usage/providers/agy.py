"""Antigravity quota, read with the Google login the Antigravity CLI (agy) already holds."""

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path

from .. import cache as files, http, oauth, paths
from . import google

NAME = "agy"
LABEL = "Antigravity"
PACKAGE = "agy-usage"
DESCRIPTION = "Read Antigravity quota with the Google login the Antigravity CLI already holds."
BASE_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal"
INTERVAL = 300
KEEP = frozenset({"id", "name", "displayName", "bucketId", "window", "disabled"})
# The quota service leaves out the Gemini group for clients older than the current agy release,
# so requests carry the installed agy version, or this one when agy is missing.
FALLBACK_VERSION = "1.4.0"
WINDOWS = {"5h": ("5-hour", 5 * 3600), "weekly": ("weekly", 7 * 86400)}


def token_file():
    return Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def agy_binary():
    return paths.executable("agy")


@cache
def _version_at(path, _mtime_ns):
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\d+\.\d+\.\d+", result.stdout)
    return match.group(0) if match else None


def user_agent():
    binary = agy_binary()
    try:
        version = _version_at(str(binary), binary.stat().st_mtime_ns) if binary else None
    except OSError:
        version = None
    return f"antigravity/cli/{version or FALLBACK_VERSION} (aidev_client; os_type=linux; arch=amd64)"


def read_auth(path=None):
    path = path or token_file()
    auth = files.read_json(path)
    if not auth:
        raise RuntimeError(f"Antigravity is not signed in: no token at {path}")
    return auth


def token_payload(auth):
    payload = auth.get("token")
    return payload if isinstance(payload, dict) else auth


def identity(auth):
    payload = token_payload(auth)
    return payload.get("access_token"), payload.get("refresh_token") or auth.get("refresh_token")


def account(path=None):
    """A one-way fingerprint of the Google account, by default from agy's own token file."""
    auth = read_auth(path)
    # Since about 2026-10-23 agy keeps the account's email instead of its id token.
    return google.account_id(auth.get("id_token"), auth.get("id_token_email"), identity(auth)[1])


def client_candidates():
    """The OAuth client ids and secrets compiled into agy, newest first."""
    binary = agy_binary()
    if not binary:
        raise RuntimeError("The Antigravity token expired and agy, which holds its OAuth client, was not found")
    try:
        data = binary.read_bytes()
    except OSError:
        raise RuntimeError(f"The Antigravity token expired and {binary} could not be read") from None
    ids = [match.group(0).decode() for match in re.finditer(rb"\d+-[a-z0-9]+\.apps\.googleusercontent\.com", data)]
    secrets = [match.group(0).decode() for match in re.finditer(re.escape(b"GO" + b"CSPX-") + rb"[A-Za-z0-9_-]{28}", data)]
    candidates = list(dict.fromkeys((client, secret) for client in reversed(ids) for secret in secrets))
    if not candidates:
        raise RuntimeError("The Antigravity token expired and agy holds no OAuth client")
    return candidates


def refresh(auth):
    """Renew the access token, unless agy already renewed it, and save it where agy keeps it."""
    expected = identity(auth)
    refresh_token = expected[1]
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError(f"No refresh token in {token_file()}")
    for client_id, secret in client_candidates():
        try:
            result = oauth.google_refresh(refresh_token, client_id, secret)
            break
        except http.StatusError as exc:
            if exc.code not in (400, 401):
                raise
            latest = files.read_json(token_file())
            if latest and identity(latest) != expected:
                return latest
    else:
        raise RuntimeError("Google rejected the Antigravity refresh token with every OAuth client agy holds")
    payload = dict(token_payload(auth))
    payload.update(access_token=result["access_token"], token_type=result.get("token_type", payload.get("token_type", "Bearer")))
    if result.get("refresh_token"):
        payload["refresh_token"] = result["refresh_token"]
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(result.get("expires_in", 3600)))
    payload["expiry"] = expiry.isoformat().replace("+00:00", "Z")
    updated = {**auth, "token": payload} if isinstance(auth.get("token"), dict) else {**auth, **payload}
    return oauth.replace_if_unchanged(token_file(), expected, identity, updated)


def expires_soon(auth):
    try:
        expiry = datetime.fromisoformat(token_payload(auth)["expiry"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return False
    return (expiry - datetime.now(timezone.utc)).total_seconds() < 60


def access_token(force=False):
    auth = read_auth()
    if force or expires_soon(auth):
        auth = refresh(auth)
    token = token_payload(auth).get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"No access token in {token_file()}")
    return token


def fetch():
    """The plan tiers of loadCodeAssist and the quota summary; the project id is left out."""
    token = access_token()
    for attempt in range(2):
        try:
            load = google.code_assist(BASE_URL, "loadCodeAssist", {"metadata": {"ideType": "ANTIGRAVITY"}}, token, user_agent())
            project = load.get("cloudaicompanionProject")
            if not project:
                raise RuntimeError("loadCodeAssist returned no Antigravity project")
            summary = google.code_assist(BASE_URL, "retrieveUserQuotaSummary", {"project": project}, token, user_agent())
            return {"load": google.tiers(load), "summary": summary}
        except http.StatusError as exc:
            if exc.code not in (401, 403) or attempt:
                raise
            token = access_token(force=True)


def normalize(payload):
    """One window per bucket of each quota group, scoped to the group's models."""
    groups = (payload.get("summary") or {}).get("groups")
    if not isinstance(groups, list):
        raise ValueError("Invalid quota summary: groups must be a list")
    windows, disabled = [], []
    for group in groups:
        buckets = group.get("buckets") if isinstance(group, dict) else None
        if not isinstance(buckets, list):
            raise ValueError("Invalid quota summary: group buckets must be a list")
        scope = str(group.get("displayName") or "Quota")
        if scope.lower().endswith(" models"):
            scope = scope[:-len(" models")]
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise ValueError("Invalid quota summary: each bucket must be an object")
            window = bucket.get("window")
            name, seconds = WINDOWS.get(window, (str(window or bucket.get("bucketId") or "quota"), None))
            if bucket.get("disabled"):
                disabled.append(f"{scope} {name}")
                continue
            if "remainingFraction" not in bucket:
                continue
            windows.append({"name": name, "seconds": seconds, "scope": scope,
                            "used_pct": round(100 - google.fraction(bucket["remainingFraction"]) * 100, 6),
                            "resets_at": google.iso(bucket.get("resetTime"))})
    return {"plan": google.plan(payload.get("load") or {}), "windows": windows,
            "details": {"disabled": disabled} if disabled else {}}
