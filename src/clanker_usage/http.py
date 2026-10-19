"""Authenticated JSON requests that never follow redirects and never echo secrets."""

import json
import urllib.error
import urllib.parse
import urllib.request
from importlib.metadata import PackageNotFoundError, version

LIMIT = 1 << 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # A redirect surfaces as an HTTPError, so credentials never reach another URL.
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class StatusError(RuntimeError):
    """An HTTP error status, kept as .code so a caller can retry after renewing a token."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def user_agent():
    try:
        return f"clanker-usage/{version('clanker-usage')}"
    except PackageNotFoundError:
        return "clanker-usage"


def request_json(url, headers, body=None, form=None, timeout=15):
    """GET url, or POST body as JSON or form as a URL-encoded form, and return the decoded response.

    Errors name only the status or failure kind, never a header, body or credential.
    """
    headers = {"User-Agent": user_agent(), "Accept": "application/json", **headers}
    if not all(value.isprintable() for value in headers.values()):
        raise RuntimeError("Invalid credential: contains control characters")
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with _opener.open(request, timeout=timeout) as response:
            raw = response.read(LIMIT + 1)
    except urllib.error.HTTPError as exc:
        # Never include response bodies or request headers in diagnostics.
        if exc.code in (401, 403):
            raise StatusError(f"Credential rejected (HTTP {exc.code}); sign in again with the provider's own client",
                              exc.code) from None
        raise StatusError(f"Usage request failed (HTTP {exc.code})", exc.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Could not connect to the usage API") from None
    if len(raw) > LIMIT:
        raise RuntimeError("Usage response exceeds 1 MiB")
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError("Usage response is not valid JSON") from None
