"""Renewing an OAuth access token that the owning client keeps in a file.

A provider renews the token only when the owning client left it expired, and replaces the file
only if nobody changed it meanwhile, so a renewal the owning client completed is never undone.
"""

from . import cache, http

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def google_refresh(refresh_token, client_id, client_secret):
    """A new access token from Google's token endpoint. A rejected refresh raises http.StatusError."""
    return http.request_json(GOOGLE_TOKEN_URL, {}, form={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": client_secret,
    })


def replace_if_unchanged(path, expected, identity, updated):
    """Save updated at path unless the file's identity is no longer expected; return what the file now holds.

    identity maps the file's contents, such as its refresh and access tokens, to something comparable.
    """
    latest = cache.read_json(path)
    if latest is not None and identity(latest) != expected:
        return latest
    cache.write_json(path, updated)
    return updated
