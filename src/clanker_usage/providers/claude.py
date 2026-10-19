"""Claude subscription limits, read with the login Claude Code already holds."""

import contextlib
import contextvars
import hashlib
import json
import math
import os
import time
from datetime import timezone, datetime
from pathlib import Path

from .. import cache as files, http, oauth, paths
from ..errors import ExpectedUnavailable

NAME = "claude"
LABEL = "Claude"
PACKAGE = "ccusage"
DESCRIPTION = "Read Claude subscription limits with the login Claude Code already holds."
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public OAuth client
# Anthropic's edge rejects other User-Agents with a 403.
USER_AGENT = "claude-code/2.1.71"
INTERVAL = 300
KEEP = frozenset({"kind", "display_name", "id", "is_enabled"})
KINDS = {"session": ("5-hour", 5 * 3600), "weekly_all": ("weekly", 7 * 86400), "weekly_scoped": ("weekly", 7 * 86400)}

# The config directory being read, for the all-accounts view, which reads several at once.
_config_dir = contextvars.ContextVar("config_dir", default=None)


def config_dir():
    selected = _config_dir.get() or os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(selected) if selected else Path.home() / ".claude"


@contextlib.contextmanager
def using(directory):
    """Read the account of another config directory, as CLAUDE_CONFIG_DIR would select it."""
    token = _config_dir.set(directory)
    try:
        yield
    finally:
        _config_dir.reset(token)


def selectors():
    """Every config directory to show: None for the current one, which needs no override, and those
    added with configure --config-dir."""
    current = config_dir().resolve()
    extra = [str(Path(directory)) for directory in paths.config(NAME).get("config_dirs", [])]
    return [None, *dict.fromkeys(directory for directory in extra if Path(directory).resolve() != current)]


def credentials_file():
    return config_dir() / ".credentials.json"


def account_file():
    # Claude Code keeps .claude.json in the home directory, or in CLAUDE_CONFIG_DIR when that is set.
    selected = _config_dir.get() or os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(selected) if selected else Path.home()) / ".claude.json"


def account_uuid():
    try:
        uuid = (json.loads(account_file().read_text()).get("oauthAccount") or {}).get("accountUuid")
    except (OSError, ValueError, AttributeError):
        uuid = None
    if not isinstance(uuid, str) or not uuid:
        raise RuntimeError(f"Claude Code is not signed in: no account in {account_file()}")
    return uuid


def account():
    """A one-way fingerprint of the Claude account, shared by every config directory signed into it."""
    return hashlib.sha256(account_uuid().encode()).hexdigest()[:16]


def read_credentials():
    credentials = files.read_json(credentials_file())
    token = ((credentials or {}).get("claudeAiOauth") or {}).get("accessToken")
    if not token:
        raise RuntimeError(f"Claude Code is not signed in: no token in {credentials_file()}")
    return credentials


def identity(credentials):
    login = (credentials or {}).get("claudeAiOauth") or {}
    return login.get("accessToken"), login.get("refreshToken")


def plan(credentials):
    login = credentials.get("claudeAiOauth") or {}
    tier = login.get("rateLimitTier") or login.get("subscriptionType")
    return tier.removeprefix("default_claude_") if isinstance(tier, str) and tier else None


def refresh(credentials):
    """Renew the access token, unless Claude Code already did, and save the rotated refresh token.

    Anthropic rotates refresh tokens, so the new one must reach Claude Code's file, or Claude Code
    is signed out. The file is replaced only if nobody changed it since it was read.
    """
    expected = identity(credentials)
    latest = files.read_json(credentials_file())
    if latest and identity(latest) != expected:
        return latest
    refresh_token = expected[1]
    if not refresh_token:
        raise RuntimeError("The Claude token expired and there is no refresh token; open Claude Code to sign in")
    try:
        result = http.request_json(TOKEN_URL, {"User-Agent": USER_AGENT},
                                   {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID})
    except http.StatusError as exc:
        latest = files.read_json(credentials_file())
        if exc.code in (400, 401) and latest and identity(latest) != expected:
            return latest
        raise RuntimeError(f"Token renewal failed (HTTP {exc.code}); open Claude Code to renew it") from None
    login = dict(credentials.get("claudeAiOauth") or {})
    login["accessToken"] = result["access_token"]
    if result.get("refresh_token"):
        login["refreshToken"] = result["refresh_token"]
    login["expiresAt"] = int(time.time() * 1000 + int(result.get("expires_in", 3600)) * 1000)
    return oauth.replace_if_unchanged(credentials_file(), expected, identity, {**credentials, "claudeAiOauth": login})


def _usage(token):
    try:
        return http.request_json(USAGE_URL, {"Authorization": "Bearer " + token, "User-Agent": USER_AGENT,
                                             "anthropic-beta": "oauth-2025-04-20"})
    except http.StatusError as exc:
        if exc.code == 403:
            # The edge does not say why, but this is how an account without a subscription,
            # or an organization that turned off OAuth, answers.
            raise ExpectedUnavailable("Usage unavailable: no active subscription, or the organization disabled OAuth") from None
        if exc.code == 429:
            raise RuntimeError("Usage request rate limited (HTTP 429); try again shortly") from None
        raise


def fetch():
    """The usage response and the plan from Claude Code's credentials, for the account signed in throughout."""
    before = account_uuid()
    credentials = read_credentials()
    if time.time() * 1000 > (credentials["claudeAiOauth"].get("expiresAt") or 0) - 60_000:
        credentials = refresh(credentials)
    token, renewed = identity(credentials)[0], False
    for _ in range(3):
        try:
            usage = _usage(token)
            break
        except http.StatusError as exc:
            if exc.code != 401:
                raise
            latest = files.read_json(credentials_file())
            if identity(latest)[0] and identity(latest)[0] != token:
                credentials, token = latest, identity(latest)[0]
            elif not renewed:
                credentials, renewed = refresh(latest or credentials), True
                token = identity(credentials)[0]
            else:
                raise
    else:
        raise RuntimeError("Usage request failed after renewing the token")
    if account_uuid() != before:
        raise RuntimeError("Claude Code switched accounts during the request; try again")
    return {"usage": {key: usage.get(key) for key in ("limits", "extra_usage")} if isinstance(usage, dict) else usage,
            "plan": plan(credentials)}


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("Invalid usage response: limit percent must be between 0 and 100")
    return value


def _reset(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("Invalid usage response: reset time must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("Invalid usage response: reset time must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def normalize(payload):
    """The session and weekly windows, with model-scoped weekly limits scoped to their model."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    limits = usage.get("limits") if isinstance(usage, dict) else None
    if not isinstance(limits, list):
        raise ValueError("Invalid usage response: limits must be a list")
    windows = []
    for entry in limits:
        if not isinstance(entry, dict):
            raise ValueError("Invalid usage response: each limit must be an object")
        if entry.get("percent") is None:
            continue
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("Invalid usage response: limit kind must be a non-empty string")
        scope = None
        if kind == "weekly_scoped":
            model = (entry.get("scope") or {}).get("model") if isinstance(entry.get("scope"), dict) else None
            scope = (model.get("display_name") or model.get("id")) if isinstance(model, dict) else None
            if not isinstance(scope, str) or not scope.strip():
                raise ValueError("Invalid usage response: scoped limit must identify a model")
            scope = scope.strip()
        name, seconds = KINDS.get(kind, (kind, None))
        windows.append({"name": name, "seconds": seconds, "scope": scope,
                        "used_pct": _percent(entry["percent"]), "resets_at": _reset(entry.get("resets_at"))})
    extra = usage.get("extra_usage")
    details = {"extra_usage": extra} if isinstance(extra, dict) and extra.get("is_enabled") else {}
    return {"plan": payload.get("plan"), "windows": windows, "details": details}


def add_commands(sub):
    configure = sub.add_parser("configure", help="Also show the account of another Claude config directory")
    configure.add_argument("--config-dir", required=True, help="a directory Claude Code uses through CLAUDE_CONFIG_DIR")


def run_command(args):
    if args.command != "configure":
        return False
    directory = str(Path(args.config_dir).expanduser().resolve())
    with using(directory):
        account_uuid()
    section = paths.config(NAME)
    section["config_dirs"] = list(dict.fromkeys([*section.get("config_dirs", []), directory]))
    paths.set_config(NAME, section)
    print(f"Configured {paths.config_file()}")
    return True
