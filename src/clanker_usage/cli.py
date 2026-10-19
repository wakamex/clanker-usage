"""Show the plan and quota of coding-agent accounts, read from each provider's own API."""

import argparse
import json
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import cache, paths, shapes
from .providers import PROVIDERS

CORE = "clanker-usage"
SCHEMA = "usage-v1"
ERRORS = (RuntimeError, ValueError, OSError, OverflowError)
FETCH_ERRORS = (RuntimeError, ValueError, OverflowError)
STATUSLINE_MAX_AGE = 300


def package_version(package):
    try:
        return version(package)
    except PackageNotFoundError:
        return version(CORE)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def document(provider, account, status, body=None, error=None, retrieved_at=None):
    """A usage-v1 document: the same fields for every provider, with provider-only data under details."""
    body = body or {"plan": None, "windows": [], "details": {}}
    return {
        "schema": SCHEMA, "provider": provider.NAME, "account": account, "plan": body["plan"],
        "status": status, "error": error, "retrieved_at": retrieved_at, "windows": body["windows"],
        "details": body["details"],
    }


def cached(provider, account):
    record = cache.read_json(paths.cache_file(provider.NAME, account))
    if record and record.get("schema") == SCHEMA and record.get("account") == account:
        return record
    return None


def fetch(provider, account):
    """Fetch live, save the document in the account's cache, and return it."""
    record = document(provider, account, "live", provider.normalize(provider.fetch()), retrieved_at=now())
    cache.write_json(paths.cache_file(provider.NAME, account), record)
    return record


def backoff_seconds(failures):
    """How long repeated failures pause fetching: one minute, doubling up to 30 minutes."""
    return min(60 * 2 ** (failures - 1), 1800)


def load(provider, max_age=0):
    """The provider's document: cached when younger than max_age, else live, else stale or unavailable.

    With max_age above 0, as for a statusline, a recent failure pauses fetching, so repeated
    prompts during an outage do not each retry. max_age 0 always fetches.
    """
    try:
        account = provider.account()
    except FETCH_ERRORS as exc:
        return document(provider, None, "unavailable", error=str(exc))
    previous = cached(provider, account)
    if previous and max_age > 0 and cache.is_fresh(previous, max_age):
        return {**previous, "status": "cached"}
    backoff = paths.backoff_file(provider.NAME, account)
    failure = cache.read_json(backoff) or {}
    failures = failure.get("failures") if isinstance(failure.get("failures"), int) else 0
    if max_age > 0 and failures and cache.is_fresh_at(failure, "failed_at", backoff_seconds(failures)):
        error = str(failure.get("error"))
    else:
        try:
            record = fetch(provider, account)
        except FETCH_ERRORS as exc:
            error = str(exc)
            cache.write_json(backoff, {"failed_at": now(), "failures": failures + 1, "error": error})
        else:
            backoff.unlink(missing_ok=True)
            return record
    if previous:
        return {**previous, "status": "stale", "error": error}
    return document(provider, account, "unavailable", error=error)


def short_duration(iso):
    """Time until iso, such as 2.9h or 4.7d; empty once it has passed."""
    hours = (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds() / 3600
    if hours <= 0:
        return ""
    return f"{hours / 24:.1f}d" if hours >= 48 else f"{hours:.1f}h"


def pct(value):
    """A share for display, to one decimal place at most; JSON keeps the exact value."""
    return f"{round(value, 1):g}%"


def status_note(record):
    if record["status"] == "unavailable":
        return f"unavailable: {record['error']}"
    if record["status"] == "stale":
        return f"stale, from {record['retrieved_at']}; live fetch failed: {record['error']}"
    return None


def render(provider, record):
    lines = [f"{provider.LABEL}: {record['plan'] or 'no plan'}"]
    for window in record["windows"]:
        line = f"  {window['name']}: {pct(window['used_pct'])} used"
        if window["resets_at"]:
            reset = datetime.fromisoformat(window["resets_at"]).astimezone()
            line += f", resets {reset:%Y-%m-%d %H:%M %Z}"
        lines.append(line)
    note = status_note(record)
    return "\n".join(lines + ([f"  ({note})"] if note else []))


def statusline(provider, record):
    if record["status"] == "unavailable":
        return f"{provider.LABEL} unavailable"
    parts = [f"{provider.LABEL} {record['plan'] or 'no plan'}"]
    for window in record["windows"]:
        part = f"{window['name']} {pct(window['used_pct'])}"
        until = short_duration(window["resets_at"]) if window["resets_at"] else ""
        if until:
            part += f" ({until})"
        parts.append(part)
    return " · ".join(parts) + (" (stale)" if record["status"] == "stale" else "")


def table(records):
    """One row per window of every provider's document."""
    rows = [("account", "plan", "window", "used", "resets in", "note")]
    for provider, record in records:
        name = provider.LABEL + (f" {record['account'][:8]}" if record["account"] else "")
        note = status_note(record) or ""
        if not record["windows"]:
            rows.append((name, record["plan"] or "", "-", "", "", note or ("no plan" if not record["plan"] else "")))
        for window in record["windows"]:
            rows.append((name, record["plan"] or "", window["name"], pct(window["used_pct"]),
                         short_duration(window["resets_at"]) if window["resets_at"] else "", note))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    return "\n".join(("  ".join(cell.ljust(width) for cell, width in zip(row[:5], widths)) + "  " + row[5]).rstrip()
                     for row in rows)


def check_one(provider, record_dir=None):
    """Fetch live, compare the response's structure with the recorded shapes, and save the outcome.

    The result is error, new-shape, missing (a window the previous check saw is absent) or ok.
    The saved state holds paths and window names, never values.
    """
    state_path = paths.state_file(provider.NAME)
    previous = cache.read_json(state_path) or {}
    state = {"checked_at": now(), "result": "ok", "details": [], "windows": previous.get("windows", [])}
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
        names = [window["name"] for window in provider.normalize(payload)["windows"]]
    except ERRORS as exc:
        state["result"] = "error"
        state["details"].append(str(exc))
    else:
        missing = [name for name in state["windows"] if name not in names]
        if missing and state["result"] == "ok":
            state["result"] = "missing"
        state["details"] += [f"missing window: {name}" for name in missing]
        state["windows"] = names
    cache.write_json(state_path, state)
    return state


def check(names, record_dir=None):
    failed = False
    for name in names:
        state = check_one(PROVIDERS[name], record_dir)
        print(f"{name}: {state['result']}" + "".join(f"\n  {line}" for line in state["details"]))
        failed |= state["result"] != "ok"
    return 1 if failed else 0


def daemon(provider, interval):
    """Refresh the account's cache every interval, skipping a refresh another process just made."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    backoff = interval
    while True:
        try:
            account = provider.account()
            previous = cached(provider, account)
            if not (previous and cache.is_fresh(previous, interval)):
                print(statusline(provider, fetch(provider, account)), flush=True)
            backoff = interval
        except ERRORS as exc:
            print(str(exc), file=sys.stderr, flush=True)
            backoff = min(backoff * 2, max(interval, 3600))
        time.sleep(backoff)


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def nonnegative_int(value):
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return result


def add_max_age(parser, default):
    parser.add_argument("--max-age", type=nonnegative_int, default=default, metavar="SECONDS",
                        help=f"reuse a cache younger than this; 0 always fetches (default: {default})")


def add_provider_commands(parser, provider):
    sub = parser.add_subparsers(dest="command")
    add_max_age(sub.add_parser("status", help="Show current quotas (the default)"), 0)
    add_max_age(sub.add_parser("json", help="Print the usage-v1 document"), 0)
    add_max_age(sub.add_parser("statusline", help="Print one line for a status bar"), STATUSLINE_MAX_AGE)
    sub.add_parser("refresh", help="Fetch and update the cache; fail if unavailable")
    daemon_parser = sub.add_parser("daemon", help=f"Refresh the cache every {provider.INTERVAL} seconds")
    daemon_parser.add_argument("--interval", type=positive_int, default=provider.INTERVAL, metavar="SECONDS")
    checker = sub.add_parser("check", help="Compare a live response with the recorded response shapes")
    checker.add_argument("--record", metavar="DIR", type=Path, help="also save a synthetic copy of the response in DIR")
    if hasattr(provider, "add_commands"):
        provider.add_commands(sub)


def run_provider(provider, args):
    if hasattr(provider, "run_command") and provider.run_command(args):
        return 0
    command = args.command or "status"
    if command == "daemon":
        daemon(provider, args.interval)
    if command == "check":
        return check([provider.NAME], args.record)
    if command == "refresh":
        account = provider.account()
        fetch(provider, account)
        print(f"Updated {paths.cache_file(provider.NAME, account)}")
        return 0
    # A bare provider command is status with its default; the core's own --max-age is for the all-accounts view.
    record = load(provider, args.max_age if args.command else 0)
    if command == "json":
        print(json.dumps(record, indent=2, ensure_ascii=False))
    elif command == "statusline":
        print(statusline(provider, record))
    else:
        print(render(provider, record))
    return 1 if record["status"] == "unavailable" else 0


def show_all(max_age, as_json):
    providers = list(PROVIDERS.values())
    with ThreadPoolExecutor(len(providers)) as pool:
        records = list(pool.map(lambda provider: load(provider, max_age), providers))
    if as_json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
    else:
        print(table(list(zip(providers, records))))
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
    parser.add_argument("--json", action="store_true", help="print every account's usage-v1 document")
    add_max_age(parser, STATUSLINE_MAX_AGE)
    sub = parser.add_subparsers(dest="provider", metavar="{check," + ",".join(PROVIDERS) + "}")
    checker = sub.add_parser("check", help="Check every provider's live response against its recorded shapes")
    checker.add_argument("--record", metavar="DIR", type=Path, help="also save synthetic copies of the responses in DIR")
    for name, provider in PROVIDERS.items():
        add_provider_commands(sub.add_parser(name, help=provider.DESCRIPTION), provider)
    args = parser.parse_args(argv)
    if args.provider is None:
        return guarded(show_all, args.max_age, args.json)
    if args.provider == "check":
        return guarded(check, list(PROVIDERS), args.record)
    return guarded(run_provider, PROVIDERS[args.provider], args)
