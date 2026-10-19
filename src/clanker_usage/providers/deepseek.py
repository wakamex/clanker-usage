"""DeepSeek account balance, read with the API key DeepSeek Harness uses.

The credential lookup mirrors Harness exactly: its Node-style .env parsing, its YAML credential
store with duplicate-key and permission checks, and its precedence. It refuses to send a key
that Harness routes to a custom endpoint, since that key may belong to another service.
"""

import functools
import hashlib
import os
import re
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from .. import http

NAME = "deepseek"
LABEL = "DeepSeek"
PACKAGE = "deepseek-harness-usage"
DESCRIPTION = "Read the DeepSeek account balance with the API key DeepSeek Harness uses."
BALANCE_URL = "https://api.deepseek.com/user/balance"
INTERVAL = 300
KEEP = frozenset({"currency", "is_available"})
CREDENTIAL_REF = "DEEPSEEK_API_KEY"
# The official account key, for when Harness routes DEEPSEEK_API_KEY to a custom endpoint.
EXPLICIT_KEY_ENV = "CLANKER_USAGE_DEEPSEEK_API_KEY"

_CREDENTIAL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LEGAL_API_KEY = re.compile(r"^[\x21-\x7e]+$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# A balance goes below zero when usage outruns it, as on an account that ran out.
_DECIMAL = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


@functools.cache
def _yaml():
    """PyYAML and a loader that matches Harness: no duplicate keys, and YAML 1.2 booleans only.

    PyYAML is the deepseek extra, imported on first use so the other providers work without it.
    """
    try:
        import yaml
    except ModuleNotFoundError:
        raise RuntimeError("DeepSeek needs PyYAML: install clanker-usage[deepseek] or deepseek-harness-usage") from None

    class DuplicateKey(yaml.YAMLError):
        pass

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        key: [resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver("tag:yaml.org,2002:bool",
                                 re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))

    def unique_mapping(loader, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise DuplicateKey
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise DuplicateKey from exc
            if duplicate:
                raise DuplicateKey
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    return yaml, Loader


def _expand_harness_home(path: str) -> str:
    """Mirror Harness's deliberately narrow tilde expansion."""
    if path == "~":
        return str(Path.home())
    if path.startswith(("~/", "~\\")):
        return os.path.join(Path.home(), path[2:])
    return path


def _absolute_lexical(path: str) -> Path:
    """Make a path absolute without following its final symlink."""
    return Path(os.path.abspath(_expand_harness_home(path)))


def get_dsh_home() -> Path:
    """Resolve the default Harness home from the launching environment."""
    configured = os.environ.get("DSH_HOME", "")
    return _absolute_lexical(configured) if configured.strip() else Path.home() / ".dsh"


def _read_text_if_present(path: Path, label: str) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Could not read {label} at {path}") from exc


def _trim_node_env(value: str) -> str:
    return value.strip(" \t\n")


def _parse_node_env(text: str) -> dict[str, str]:
    """Mirror the Node 22 parser used by Harness's launch environment."""
    content = _trim_node_env(text.replace("\r", ""))
    values: dict[str, str] = {}
    while content:
        if content[0] in "\n#":
            newline = content.find("\n")
            content = content[newline + 1 :] if newline >= 0 else ""
            continue

        equals = content.find("=")
        newline = content.find("\n")
        delimiter = (
            min(index for index in (equals, newline) if index >= 0)
            if (equals >= 0 or newline >= 0)
            else -1
        )
        if delimiter < 0 or (newline >= 0 and delimiter == newline):
            content = _trim_node_env(content[delimiter + 1 :]) if delimiter >= 0 else ""
            continue

        key = _trim_node_env(content[:delimiter])
        content = content[delimiter + 1 :]
        if not content or content[0] == "\n":
            values[key] = ""
            continue
        content = _trim_node_env(content)
        if not key:
            continue
        if key.startswith("export "):
            key = _trim_node_env(key[7:])
        if not content:
            values[key] = ""
            break

        if content[0] == '"':
            closing = content.find('"', 1)
            if closing >= 0:
                values[key] = content[1:closing].replace("\\n", "\n")
                newline = content.find("\n", closing + 1)
                content = content[newline + 1 :] if newline >= 0 else ""
                continue

        if content[0] in "'\"`":
            closing = content.find(content[0], 1)
            if closing < 0:
                newline = content.find("\n")
                if newline >= 0:
                    values[key] = content[:newline]
                    content = content[newline + 1 :]
                else:
                    values[key] = content
                    break
            else:
                values[key] = content[1:closing]
                newline = content.find("\n", closing + 1)
                content = content[newline + 1 :] if newline >= 0 else ""
                continue
        else:
            newline = content.find("\n")
            if newline >= 0:
                value = content[:newline]
                comment = value.find("#")
                if comment >= 0:
                    value = value[:comment]
                values[key] = _trim_node_env(value)
                content = content[newline + 1 :]
            else:
                comment = content.find("#")
                value = content[:comment] if comment >= 0 else content
                values[key] = _trim_node_env(value)
                content = ""
        content = _trim_node_env(content)
    return values


def _dotenv_lookup(path: Path, name: str) -> tuple[bool, str | None]:
    text = _read_text_if_present(path, ".env credential source")
    if text is None:
        return False, None
    values = _parse_node_env(text)
    return name in values, values.get(name)


def _dotenv_value(path: Path, name: str) -> str | None:
    found, value = _dotenv_lookup(path, name)
    return value if found and value else None


def _managed_credential(path: Path, name: str) -> tuple[int | None, str | None]:
    """The layout of Harness's credential store, 0 for flat or 1 for version 1, and the value for name."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise RuntimeError(
            f"Could not inspect Harness credential store at {path}"
        ) from exc
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise RuntimeError(
            f"Harness credential store {path} is readable beyond its owner; "
            f'run "chmod 600 {path}" before using it'
        )
    text = _read_text_if_present(path, "Harness credential store")
    if text is None:  # The file may disappear between stat and read.
        return None, None
    yaml, loader = _yaml()
    try:
        document = yaml.load(text, Loader=loader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else ""
        )
        raise RuntimeError(
            f"Harness credential store {path} is invalid{location}"
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise RuntimeError(f"Harness credential store {path} must be a mapping")
    if not document:
        return None, None
    if "version" not in document:
        # The flat layout of Harness 0.1.x: references at the top level.
        return 0, _credential_refs(document, path).get(name)
    # Harness 0.2 nests references under `refs` in a version 1 document and ignores unknown top-level keys.
    version = document["version"]
    if isinstance(version, bool) or version != 1:
        raise RuntimeError(f"Harness credential store {path} declares a version other than 1, which this tool cannot read")
    if not isinstance(document.get("records", {}), dict):
        raise RuntimeError(f"Harness credential store {path} has invalid records")
    refs = document.get("refs", {})
    if not isinstance(refs, dict):
        raise RuntimeError(f"Harness credential store {path} has invalid refs")
    return 1, _credential_refs(refs, path).get(name)


def _credential_refs(refs: dict, path: Path) -> dict:
    """References to non-empty strings, rejecting the whole store otherwise, as Harness does."""
    for key, value in refs.items():
        if not isinstance(key, str) or _CREDENTIAL_NAME.fullmatch(key) is None:
            raise RuntimeError(
                f"Harness credential store {path} has an invalid credential reference"
            )
        if not isinstance(value, str):
            raise RuntimeError(f"The value for {key} in {path} is not a string")
        if not value:
            raise RuntimeError(
                f"The value for {key} in {path} is empty; remove it instead"
            )
    return refs


def _usable_api_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(
            "The configured DEEPSEEK_API_KEY is blank; set it to the raw key"
        )
    if _LEGAL_API_KEY.fullmatch(normalized) is None:
        raise RuntimeError(
            "The configured DEEPSEEK_API_KEY contains characters an HTTP header cannot carry"
        )
    return normalized


def _is_official_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api.deepseek.com"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") in ("", "/v1", "/anthropic")
        and not parsed.query
        and not parsed.fragment
    )


def _assert_compatible_harness_route(cwd: Path) -> None:
    """Refuse known custom routes before reading their possible credential."""
    dsh_home = get_dsh_home()
    bases: list[tuple[bool, str | None]] = [
        (
            "DEEPSEEK_BASE_URL" in os.environ,
            os.environ.get("DEEPSEEK_BASE_URL"),
        )
    ]
    bases.append(_dotenv_lookup(cwd / ".env", "DEEPSEEK_BASE_URL"))
    if cwd != dsh_home:
        bases.append(_dotenv_lookup(dsh_home / ".env", "DEEPSEEK_BASE_URL"))

    settings_text = _read_text_if_present(
        dsh_home / "settings.yaml", "Harness settings"
    )
    if settings_text is not None:
        yaml, loader = _yaml()
        try:
            settings = yaml.load(settings_text, Loader=loader)
        except yaml.YAMLError as exc:
            raise RuntimeError(
                "Could not verify the DeepSeek route because Harness settings are invalid"
            ) from exc
        if settings is not None and not isinstance(settings, dict):
            raise RuntimeError(
                "Could not verify the DeepSeek route because Harness settings are invalid"
            )
        section = (settings or {}).get("llm-deepseek")
        if section is not None and not isinstance(section, dict):
            raise RuntimeError(
                "Could not verify the DeepSeek route because llm-deepseek settings are invalid"
            )
        if isinstance(section, dict):
            has_base_url = "baseURL" in section
            base_url = section.get("baseURL")
            if base_url is not None and not isinstance(base_url, str):
                raise RuntimeError(
                    "Could not verify the DeepSeek route because its baseURL is invalid"
                )
            bases.append((has_base_url, base_url))
            credential_ref = section.get("apiKeyEnv")
            if credential_ref not in (None, CREDENTIAL_REF):
                raise RuntimeError(
                    "Harness uses a custom DeepSeek credential reference; set "
                    f"{EXPLICIT_KEY_ENV} to the official DeepSeek account key for this tool"
                )

    for base_url, credential_ref in _profile_overrides(dsh_home):
        if credential_ref not in (None, CREDENTIAL_REF):
            raise RuntimeError(
                "Harness uses a custom DeepSeek credential reference; set "
                f"{EXPLICIT_KEY_ENV} to the official DeepSeek account key for this tool"
            )
        if base_url is not None:
            bases.append((True, base_url))

    if any(
        present and (not isinstance(base, str) or not _is_official_base_url(base))
        for present, base in bases
    ):
        raise RuntimeError(
            "Harness uses a custom DeepSeek endpoint; set "
            f"{EXPLICIT_KEY_ENV} to the official DeepSeek account key for this tool"
        )


def _profile_overrides(dsh_home: Path) -> list[tuple[object, object]]:
    """The baseURL and apiKeyEnv that Harness 0.2 profile patches set for a DeepSeek plugin.

    Each profile's cordis.patch.yml is a list of patch entries; one whose id names a DeepSeek
    plugin can override its endpoint or credential reference. A patch this tool cannot read, such
    as one using a JavaScript expression, fails closed.
    """
    overrides = []
    for patch in sorted((dsh_home / "profiles").glob("*/cordis.patch.yml")):
        text = _read_text_if_present(patch, "Harness profile patch")
        if not text:
            continue
        yaml, loader = _yaml()
        try:
            entries = yaml.load(text, Loader=loader)
        except yaml.YAMLError:
            raise RuntimeError(
                f"Could not verify the DeepSeek route because {patch} cannot be read; set "
                f"{EXPLICIT_KEY_ENV} to the official DeepSeek account key for this tool"
            ) from None
        for entry in entries or []:
            if not isinstance(entry, dict) or "deepseek" not in str(entry.get("id", "")):
                continue
            config = entry.get("config")
            if isinstance(config, dict):
                overrides.append((config.get("baseURL"), config.get("apiKeyEnv")))
    return overrides


def get_api_key(cwd: Path | None = None) -> str:
    """Resolve the default DeepSeek key with the precedence of the installed Harness.

    Harness 0.1 (a flat credential store): the exported DEEPSEEK_API_KEY, the store, the project's
    .env, then $DSH_HOME/.env. Harness 0.2 (a version 1 store): the store, the exported key, the
    project's .env, then $DSH_HOME/.env.
    """
    explicit = os.environ.get(EXPLICIT_KEY_ENV)
    if explicit is not None:
        return _usable_api_key(explicit)

    project_root = _absolute_lexical(str(cwd or Path.cwd()))
    _assert_compatible_harness_route(project_root)

    dsh_home = get_dsh_home()
    layout, stored = _managed_credential(dsh_home / ".credentials.yaml", CREDENTIAL_REF)
    inherited = os.environ.get(CREDENTIAL_REF)
    if layout is None:
        # No store yet: Harness 0.2 keeps profiles, 0.1 did not.
        layout = 1 if (dsh_home / "profiles").is_dir() else 0
    # Harness 0.1 ranked an exported key above its store; 0.2 ranks the store first.
    for value in ((inherited, stored) if layout == 0 else (stored, inherited)):
        if value:
            return _usable_api_key(value)

    project = _dotenv_value(project_root / ".env", CREDENTIAL_REF)
    if project:
        return _usable_api_key(project)
    if project_root != dsh_home:
        user = _dotenv_value(dsh_home / ".env", CREDENTIAL_REF)
        if user:
            return _usable_api_key(user)
    raise RuntimeError(
        "DeepSeek Harness has no DEEPSEEK_API_KEY; configure it in the Models page, "
        "export it, or add it to a Harness credential source"
    )


def account():
    """A one-way fingerprint of the key."""
    return hashlib.sha256(get_api_key().encode()).hexdigest()[:16]


def fetch():
    return http.request_json(BALANCE_URL, {"Authorization": "Bearer " + get_api_key()})


def _money(value, field):
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise ValueError(f"DeepSeek balance response has invalid {field}")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"DeepSeek balance response has invalid {field}") from None


def normalize(payload):
    """Each currency's balance; is_available says whether the balance still allows requests."""
    available = payload.get("is_available") if isinstance(payload, dict) else None
    raw_balances = payload.get("balance_infos") if isinstance(payload, dict) else None
    if not isinstance(available, bool) or not isinstance(raw_balances, list):
        raise ValueError("DeepSeek balance API returned an unexpected response")
    balances, parts = [], []
    for raw in raw_balances:
        currency = raw.get("currency") if isinstance(raw, dict) else None
        if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
            raise ValueError("DeepSeek balance response omitted currency")
        if currency in {item["currency"] for item in balances}:
            raise ValueError(f"DeepSeek balance response repeats currency {currency}")
        total = _money(raw.get("total_balance"), "total_balance")
        balances.append({"currency": currency, "amount": float(total)})
        parts.append({"currency": currency, "total": str(total),
                      "granted": str(_money(raw.get("granted_balance"), "granted_balance")),
                      "topped_up": str(_money(raw.get("topped_up_balance"), "topped_up_balance"))})
    balances.sort(key=lambda item: item["currency"])
    parts.sort(key=lambda item: item["currency"])
    return {"plan": "pay per use", "windows": [], "balance": balances,
            "details": {"available": available, "balances": parts}}
