"""Where clanker-usage keeps its cache, check state and configuration."""

import os
import shutil
from pathlib import Path

from . import cache


def _base(variable, default):
    return Path(os.environ.get(variable) or Path.home() / default) / "clanker-usage"


def cache_file(provider, account):
    return _base("XDG_CACHE_HOME", ".cache") / provider / f"{account}.json"


def state_file(provider):
    return _base("XDG_STATE_HOME", ".local/state") / "check" / f"{provider}.json"


def backoff_file(provider, account):
    return _base("XDG_STATE_HOME", ".local/state") / "backoff" / provider / f"{account}.json"


def config_file():
    return _base("XDG_CONFIG_HOME", ".config") / "config.json"


def config(provider):
    """The provider's section of the configuration file, empty when unset."""
    section = (cache.read_json(config_file()) or {}).get(provider)
    return section if isinstance(section, dict) else {}


def set_config(provider, section):
    data = cache.read_json(config_file()) or {}
    data[provider] = section
    cache.write_json(config_file(), data)


def executable(name):
    """A harness's command: on PATH, else where user installers put it.

    A systemd user service runs with a PATH that leaves out ~/.local/bin, where Claude Code, Devin,
    agy and Gemini CLI install themselves, so PATH alone misses them under a daemon.
    """
    if found := shutil.which(name):
        return Path(found)
    for folder in (Path.home() / ".local/bin", Path.home() / "bin", Path("/usr/local/bin"), Path("/usr/bin")):
        candidate = folder / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None
