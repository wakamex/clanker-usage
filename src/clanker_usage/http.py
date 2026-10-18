"""Authenticated JSON requests that never follow redirects and never echo secrets."""

import json
import urllib.error
import urllib.request

LIMIT = 1 << 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # A redirect surfaces as an HTTPError, so credentials never reach another URL.
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def get_json(url, headers, timeout=15):
    """GET url and return its decoded JSON body. Errors name only the status or failure kind."""
    if not all(value.isprintable() for value in headers.values()):
        raise RuntimeError("Invalid credential: contains control characters")
    request = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(request, timeout=timeout) as response:
            body = response.read(LIMIT + 1)
    except urllib.error.HTTPError as exc:
        # Never include response bodies or request headers in diagnostics.
        raise RuntimeError(f"Usage request failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Could not connect to the usage API") from None
    if len(body) > LIMIT:
        raise RuntimeError("Usage response exceeds 1 MiB")
    try:
        return json.loads(body)
    except ValueError:
        raise ValueError("Usage response is not valid JSON") from None
