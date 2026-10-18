"""Show the plan and quota of coding-agent accounts, read from each provider's own API."""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import cache, shapes
from .providers import PROVIDERS

CORE = "clanker-usage"
SCHEMA = "usage-v1"
ERRORS = (RuntimeError, ValueError, OSError, OverflowError)


def package_version(package):
    try:
        return version(package)
    except PackageNotFoundError:
        return version(CORE)


def load(provider, cached=False):
    """The provider's current record and its usage-v1 status: live, cached or stale."""
    previous = cache.read_json(provider.cache_path())
    if cached and previous and cache.is_fresh(previous, provider.INTERVAL):
        return previous, "cached"
    try:
        record = provider.normalize(provider.fetch())
    except (RuntimeError, ValueError, OverflowError):
        if not previous:
            raise
        return {**previous, "stale": True}, "stale"
    cache.write_json(provider.cache_path(), record)
    return record, "live"


def summary(provider, record, status):
    return {
        "schema": SCHEMA, "account": provider.NAME, "plan": record.get("plan"), "status": status,
        "error": None, "retrieved_at": record.get("updated_at"), "windows": provider.windows(record),
    }


def state_path(provider):
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "clanker-usage/check" / f"{provider.NAME}.json"


def check_one(provider, record_dir=None):
    """Fetch live, compare the response's structure with the recorded shapes, and save the outcome.

    The result is error, new-shape, missing (a window the previous check saw is absent) or ok.
    The saved state holds paths and window names, never values.
    """
    previous = cache.read_json(state_path(provider)) or {}
    state = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "result": "ok", "details": [], "windows": previous.get("windows", [])}
    try:
        payload = provider.fetch()
        if record_dir:
            path = Path(record_dir) / f"{provider.NAME}-{datetime.now(timezone.utc):%Y-%m-%d}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(shapes.synthetic(payload, provider.KEEP), indent=2, ensure_ascii=False) + "\n")
            state["details"].append(f"recorded {path}")
        new = sorted(shapes.paths(payload) - shapes.known_paths(provider.NAME))
        if new:
            state.update(result="new-shape")
            state["details"] += new
        names = [window["name"] for window in provider.windows(provider.normalize(payload))]
    except ERRORS as exc:
        state["result"] = "error"
        state["details"].append(str(exc))
    else:
        missing = [name for name in state["windows"] if name not in names]
        if missing and state["result"] == "ok":
            state["result"] = "missing"
        state["details"] += [f"missing window: {name}" for name in missing]
        state["windows"] = names
    cache.write_json(state_path(provider), state)
    return state


def check(names, record_dir=None):
    failed = False
    for name in names:
        state = check_one(PROVIDERS[name], record_dir)
        print(f"{name}: {state['result']}" + "".join(f"\n  {line}" for line in state["details"]))
        failed |= state["result"] != "ok"
    return 1 if failed else 0


def positive_interval(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("interval must be positive")
    return result


def add_provider_commands(parser, provider):
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Fetch and show current quotas (the default)")
    sub.add_parser("json", help="Normalized JSON with a usage-v1 summary")
    sub.add_parser("refresh", help="Fetch and update the cache; fail if unavailable")
    sub.add_parser("statusline", help=f"One line; reuse a cache less than {provider.INTERVAL} seconds old")
    daemon = sub.add_parser("daemon", help=f"Refresh the cache every {provider.INTERVAL} seconds")
    daemon.add_argument("--interval", "-i", type=positive_interval, default=provider.INTERVAL)
    checker = sub.add_parser("check", help="Compare a live response with the recorded response shapes")
    checker.add_argument("--record", metavar="DIR", type=Path, help="also save a synthetic copy of the response in DIR")
    provider.add_commands(sub)


def run_provider(provider, args):
    if provider.run_command(args):
        return 0
    command = args.command or "status"
    if command == "daemon":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        backoff = args.interval
        while True:
            try:
                record = provider.normalize(provider.fetch())
                cache.write_json(provider.cache_path(), record)
                print(provider.display(record, compact=True))
                backoff = args.interval
            except ERRORS as exc:
                print(str(exc), file=sys.stderr, flush=True)
                backoff = min(backoff * 2, max(args.interval, 3600))
            sys.stdout.flush()
            time.sleep(backoff)
    if command == "refresh":
        cache.write_json(provider.cache_path(), provider.normalize(provider.fetch()))
        print(f"Updated {provider.cache_path()}")
    elif command == "check":
        return check([provider.NAME], args.record)
    else:
        record, status = load(provider, cached=command == "statusline")
        if command == "json":
            print(json.dumps({**record, "summary": summary(provider, record, status)}, indent=2))
        else:
            print(provider.display(record, compact=command == "statusline"))
    return 0


def guarded(function, *args):
    try:
        return function(*args)
    except ERRORS as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


def run(name, argv=None):
    """The command of one provider's package, such as zcode-cli-usage."""
    provider = PROVIDERS[name]
    parser = argparse.ArgumentParser(prog=provider.PACKAGE, description=provider.DESCRIPTION)
    parser.add_argument("--version", action="version", version=package_version(provider.PACKAGE))
    add_provider_commands(parser, provider)
    return guarded(run_provider, provider, parser.parse_args(argv))


def main(argv=None):
    parser = argparse.ArgumentParser(prog=CORE, description=__doc__)
    parser.add_argument("--version", action="version", version=version(CORE))
    sub = parser.add_subparsers(dest="provider", required=True, metavar="{check," + ",".join(PROVIDERS) + "}")
    checker = sub.add_parser("check", help="Check every provider's live response against its recorded shapes")
    checker.add_argument("--record", metavar="DIR", type=Path, help="also save synthetic copies of the responses in DIR")
    for name, provider in PROVIDERS.items():
        add_provider_commands(sub.add_parser(name, help=provider.DESCRIPTION), provider)
    args = parser.parse_args(argv)
    if args.provider == "check":
        return guarded(check, list(PROVIDERS), args.record)
    return guarded(run_provider, PROVIDERS[args.provider], args)
