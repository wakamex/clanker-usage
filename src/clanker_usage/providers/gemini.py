"""Gemini CLI model quotas, read with the Google login Gemini CLI already holds."""

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from .. import cache as files, http, oauth
from ..errors import ExpectedUnavailable
from . import google

NAME = "gemini"
LABEL = "Gemini CLI"
PACKAGE = "gemini-cli-usage"
DESCRIPTION = "Read Gemini CLI model quotas with the Google login Gemini CLI already holds."
BASE_URL = "https://cloudcode-pa.googleapis.com/v1internal"
INTERVAL = 300
KEEP = frozenset({"id", "name", "modelId", "tokenType"})
AUTH_LABELS = {
    "oauth-personal": "Google login", "gemini-api-key": "Gemini API key", "vertex-ai": "Vertex AI",
    "cloud-shell": "Cloud Shell", "compute-default-credentials": "Compute ADC", "gateway": "Gateway",
}
# oauth2.js in older releases declares `const ... = '...'`; the bundle since about 0.60 declares `var ... = "..."`.
CLIENT_ID = re.compile(r"""(?:const|var) OAUTH_CLIENT_ID = (['"])([^'"]+)\1;""")
CLIENT_SECRET = re.compile(r"""(?:const|var) OAUTH_CLIENT_SECRET = (['"])([^'"]+)\1;""")


def gemini_dir():
    return Path(os.environ.get("GEMINI_CLI_HOME") or Path.home()) / ".gemini"


def oauth_file():
    return gemini_dir() / "oauth_creds.json"


def _selected_type(path):
    auth = (((files.read_json(path) or {}).get("security") or {}).get("auth") or {})
    selected = auth.get("selectedType") if isinstance(auth, dict) else None
    return selected if isinstance(selected, str) else None


def auth_type():
    """The login Gemini CLI would use here: its environment, then this directory's settings, then the user's."""
    env = os.environ
    if env.get("GOOGLE_GENAI_USE_GCA") == "true":
        return "oauth-personal"
    if env.get("GOOGLE_GENAI_USE_VERTEXAI") == "true":
        return "vertex-ai"
    if env.get("GEMINI_API_KEY"):
        return "gemini-api-key"
    if env.get("CLOUD_SHELL") == "true" or env.get("GEMINI_CLI_USE_COMPUTE_ADC") == "true":
        return "compute-default-credentials"
    return _selected_type(Path.cwd() / ".gemini" / "settings.json") or _selected_type(gemini_dir() / "settings.json")


def read_credentials():
    kind = auth_type()
    if kind != "oauth-personal":
        raise ExpectedUnavailable(f"Quota needs a Google login; Gemini CLI uses {AUTH_LABELS.get(kind or '', kind or 'no login')}")
    credentials = files.read_json(oauth_file())
    if not credentials:
        raise RuntimeError(f"Gemini CLI is not signed in: no credentials at {oauth_file()}")
    return credentials


def account():
    credentials = read_credentials()
    return google.account_id(credentials.get("id_token"), credentials.get("refresh_token"))


def _oauth2_sources():
    """The files that hold Gemini CLI's OAuth client: its bundle, or oauth2.js where npm and fnm install older releases."""
    relative = [Path("node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js"),
                Path("node_modules/@google/gemini-cli/node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js")]
    roots = []
    if binary := shutil.which("gemini"):
        resolved = Path(binary).resolve()
        if resolved.parent.name == "bundle":
            yield from sorted(resolved.parent.glob("*.js"))
        roots += [resolved.parent, resolved.parent.parent]
    if sys.platform == "win32":
        for folder in (Path.home() / "AppData/Roaming/fnm/node-versions", Path.home() / "AppData/Local/fnm_multishells"):
            if folder.exists():
                roots += [child / "installation" if folder.name == "node-versions" else child for child in folder.iterdir()]
    else:
        roots += [Path("/usr/lib"), Path("/usr/local/lib")]
        nvm = Path.home() / ".nvm" / "versions"
        if nvm.exists():
            roots += [node / "lib" for node in nvm.rglob("node")]
    for root in roots:
        for path in relative:
            for candidate in (root / path, root / "lib" / path):
                if candidate.exists():
                    yield candidate


def client(credentials):
    """The OAuth client Gemini CLI uses: from its credentials file, else from its installed source."""
    client_id, secret = credentials.get("client_id"), credentials.get("client_secret")
    if isinstance(client_id, str) and client_id and isinstance(secret, str) and secret:
        return client_id, secret
    for source in _oauth2_sources():
        try:
            text = source.read_text()
        except OSError:
            continue
        if (found_id := CLIENT_ID.search(text)) and (found_secret := CLIENT_SECRET.search(text)):
            return found_id.group(2), found_secret.group(2)
    raise RuntimeError("The Gemini CLI token expired and Gemini CLI's OAuth client was not found; run gemini and retry")


def refresh(credentials):
    """Renew the access token, unless Gemini CLI already renewed it, and save it where Gemini CLI keeps it."""
    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(f"No refresh token in {oauth_file()}")
    identity = lambda data: data.get("refresh_token")
    try:
        result = oauth.google_refresh(refresh_token, *client(credentials))
    except http.StatusError as exc:
        latest = files.read_json(oauth_file())
        if exc.code in (400, 401) and latest and identity(latest) not in (None, refresh_token):
            return latest
        raise
    updated = {**credentials, "access_token": result["access_token"],
               "token_type": result.get("token_type", credentials.get("token_type", "Bearer")),
               "scope": result.get("scope", credentials.get("scope")),
               "expiry_date": int(time.time() * 1000 + int(result.get("expires_in", 3600)) * 1000)}
    for key in ("id_token", "refresh_token"):
        if result.get(key):
            updated[key] = result[key]
    return oauth.replace_if_unchanged(oauth_file(), refresh_token, identity, updated)


def access_token(force=False, rejected=None):
    credentials = read_credentials()
    token = credentials.get("access_token")
    replaced = rejected is not None and isinstance(token, str) and token and token != rejected
    expiry = int(credentials.get("expiry_date", 0) or 0)
    if not replaced and (force or time.time() * 1000 >= expiry - 60_000):
        credentials = refresh(credentials)
        token = credentials.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"No access token in {oauth_file()}")
    return token


def fetch():
    """The plan tiers of loadCodeAssist and the per-model quota; the project id is left out."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT_ID") or None
    metadata = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
    if project:
        metadata["duetProject"] = project
    token = access_token()
    for attempt in range(2):
        try:
            load = google.code_assist(BASE_URL, "loadCodeAssist", {"cloudaicompanionProject": project, "metadata": metadata}, token)
            project_id = load.get("cloudaicompanionProject") or project
            if not project_id:
                raise RuntimeError("No Code Assist project; set GOOGLE_CLOUD_PROJECT if the account needs one")
            quota = google.code_assist(BASE_URL, "retrieveUserQuota", {"project": project_id}, token)
            return {"load": google.tiers(load), "quota": quota}
        except http.StatusError as exc:
            if exc.code not in (401, 403) or attempt:
                raise
            token = access_token(force=True, rejected=token)


def normalize(payload):
    """One window per model, as Gemini CLI reads its quota: buckets without a model or fraction are skipped."""
    buckets = (payload.get("quota") or {}).get("buckets")
    if not isinstance(buckets, list):
        raise ValueError("Invalid quota response: buckets must be a list")
    windows, models = [], {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise ValueError("Invalid quota response: each bucket must be an object")
        model, remaining = bucket.get("modelId"), bucket.get("remainingFraction")
        if not model or remaining is None:
            continue
        if not isinstance(model, str):
            raise ValueError("Invalid quota response: model id must be a string")
        windows.append({"name": model, "seconds": None, "scope": model,
                        "used_pct": round(100 - google.fraction(remaining) * 100, 6),
                        "resets_at": google.iso(bucket.get("resetTime"))})
        amount = bucket.get("remainingAmount")
        models[model] = {"remaining": int(amount) if isinstance(amount, str) and amount.isdigit() else None,
                         "token_type": bucket.get("tokenType")}
    return {"plan": google.plan(payload.get("load") or {}), "windows": windows, "details": {"models": models}}
