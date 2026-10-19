"""What Gemini CLI and Antigravity share: Google's Code Assist API and Google account ids."""

import base64
import hashlib
import json
from datetime import timezone, datetime

from .. import http


def account_id(id_token, fallback):
    """A one-way fingerprint of the Google account in an OpenID token, else of fallback, such as a refresh token."""
    try:
        subject = json.loads(base64.urlsafe_b64decode(id_token.split(".")[1] + "=="))["sub"]
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        subject = None
    source = subject or fallback
    if not isinstance(source, str) or not source:
        raise RuntimeError("Google credentials have neither an id token nor a refresh token")
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def code_assist(base_url, method, payload, access_token, user_agent=None):
    headers = {"Authorization": "Bearer " + access_token}
    if user_agent:
        headers["User-Agent"] = user_agent
    result = http.request_json(f"{base_url}:{method}", headers, payload)
    if not isinstance(result, dict):
        raise ValueError(f"Code Assist {method} returned an unexpected response")
    return result


def tiers(load):
    """The plan tiers of a loadCodeAssist response, keeping only their ids and names."""
    return {key: {field: tier.get(field) for field in ("id", "name") if field in tier}
            for key in ("currentTier", "paidTier") if isinstance(tier := load.get(key), dict)}


def plan(load):
    for key in ("paidTier", "currentTier"):
        name = (load.get(key) or {}).get("name")
        if isinstance(name, str) and name:
            return name
    return None


def fraction(value):
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ValueError("Invalid quota response: remaining fraction must be between 0 and 1")
    return value


def iso(value):
    """An ISO 8601 UTC time from the API's reset time, or None."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("Invalid quota response: malformed reset time") from None
    if parsed.tzinfo is None:
        raise ValueError("Invalid quota response: reset time without a time zone")
    return parsed.astimezone(timezone.utc).isoformat()
