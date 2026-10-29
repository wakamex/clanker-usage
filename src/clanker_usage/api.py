"""The public Python API: read cached usage-v1 documents and find which account a credential home holds.

Both functions read local files only; neither makes a network request. The daemons, such as
`ccusage daemon`, keep the cached documents current.
"""

from pathlib import Path

from . import cache, paths
from .cli import SCHEMA
from .providers import PROVIDERS, agy, claude, codex


class AccountUnknown(RuntimeError):
    """The account a credential home holds cannot be told: no login, or unreadable or malformed files."""


# The file in a credential home that names its account, per provider.
CREDENTIAL_FILES = {
    "claude": claude.account_file_in,
    "codex": lambda home: Path(home) / "auth.json",
    "agy": lambda home: Path(home) / "antigravity-oauth-token",
}


def _agy_account(path):
    # agy's own cache falls back to a fingerprint of the refresh token, which changes when the token
    # rotates and so names no stable account; here that is not knowing.
    auth = agy.read_auth(path)
    if not auth.get("id_token") and not auth.get("id_token_email"):
        raise RuntimeError(f"no id token or email in {path}, only a refresh token")
    return agy.account(path)


ACCOUNT_READERS = {"claude": claude.account, "codex": codex.account, "agy": _agy_account}


def read(provider, account):
    """The cached usage-v1 document for one account of a provider, or None when there is none.

    None means no usable document: never cached, or a file that is unreadable, not usage-v1 or for
    another account. A document whose status is "unavailable" is returned, since it is a reading:
    the account exists but has no quota to read, such as no subscription.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; known: {', '.join(sorted(PROVIDERS))}")
    record = cache.read_json(paths.cache_file(provider, account))
    if not record or record.get("schema") != SCHEMA or record.get("provider") != provider or record.get("account") != account:
        return None
    return record


def documents(provider):
    """Every cached usage-v1 document of a provider, one per account."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; known: {', '.join(sorted(PROVIDERS))}")
    folder = paths.cache_file(provider, "x").parent
    found = []
    for file in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        if record := read(provider, file.stem):
            found.append(record)
    return found


def account(provider, home):
    """The account id that the credential home `home` is signed into, as read() and the cache name it.

    `home` is the directory holding the provider's credential file:
    - claude: the config directory Claude Code uses, CLAUDE_CONFIG_DIR or ~/.claude, whose .claude.json
      names the account (for ~/.claude, Claude Code keeps it at ~/.claude.json);
    - codex: CODEX_HOME or ~/.codex, holding auth.json;
    - agy: the directory holding antigravity-oauth-token, ~/.gemini/antigravity-cli by default.

    Raises AccountUnknown when the home has no login or its files cannot be read, and ValueError for a
    provider whose account is not tied to a credential home.
    """
    if provider not in CREDENTIAL_FILES:
        raise ValueError(f"account() supports {', '.join(sorted(CREDENTIAL_FILES))}, not {provider!r}")
    path = CREDENTIAL_FILES[provider](home)
    try:
        return ACCOUNT_READERS[provider](path)
    except (RuntimeError, ValueError, OSError) as exc:
        raise AccountUnknown(f"{provider}: cannot tell the account in {home}: {exc}") from None
