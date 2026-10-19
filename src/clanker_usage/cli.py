"""Show the plan and quota of coding-agent accounts, read from each provider's own API."""

import argparse
import contextlib
import json
import os
import select
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import cache, paths, shapes
from .errors import ExpectedUnavailable
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
        "balance": body.get("balance"), "details": body["details"],
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


def unavailable(provider, account, error):
    """Save that the account has no quota to read, replacing its old windows, so they are not shown as current."""
    record = document(provider, account, "unavailable", error=error, retrieved_at=now())
    cache.write_json(paths.cache_file(provider.NAME, account), record)
    paths.backoff_file(provider.NAME, account).unlink(missing_ok=True)
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
        return previous if previous["status"] == "unavailable" else {**previous, "status": "cached"}
    backoff = paths.backoff_file(provider.NAME, account)
    failure = cache.read_json(backoff) or {}
    failures = failure.get("failures") if isinstance(failure.get("failures"), int) else 0
    if max_age > 0 and failures and cache.is_fresh_at(failure, "failed_at", backoff_seconds(failures)):
        error = str(failure.get("error"))
    else:
        try:
            record = fetch(provider, account)
        except ExpectedUnavailable as exc:
            return unavailable(provider, account, str(exc))
        except FETCH_ERRORS as exc:
            error = str(exc)
            cache.write_json(backoff, {"failed_at": now(), "failures": failures + 1, "error": error})
        else:
            backoff.unlink(missing_ok=True)
            return record
    if previous and previous["status"] != "unavailable":
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


def label(window):
    """The window's name, after its scope when the scope adds something, such as "Gemini weekly"."""
    scope = window.get("scope")
    return f"{scope} {window['name']}" if scope and scope != window["name"] else window["name"]


def money(item):
    return f"{item['amount']:.2f} {item['currency']}"


def status_note(record):
    if record["status"] == "unavailable":
        return f"unavailable: {record['error']}"
    if record["status"] == "stale":
        return f"stale, from {record['retrieved_at']}; live fetch failed: {record['error']}"
    return None


def render(provider, record):
    lines = [f"{provider.LABEL}: {record['plan'] or 'no plan'}"]
    lines += [f"  balance: {money(item)}" for item in record.get("balance") or []]
    for window in record["windows"]:
        line = f"  {label(window)}: {pct(window['used_pct'])} used"
        if window["resets_at"]:
            reset = datetime.fromisoformat(window["resets_at"]).astimezone()
            line += f", resets {reset:%Y-%m-%d %H:%M %Z}"
        lines.append(line)
    note = status_note(record)
    return "\n".join(lines + ([f"  ({note})"] if note else []))


def harness_prefix(status):
    """The directory, model and cost from the status JSON a harness pipes to its statusline, as Claude Code does."""
    if not isinstance(status, dict):
        return ""
    parts = []
    directory = (status.get("workspace") or {}).get("current_dir") if isinstance(status.get("workspace"), dict) else None
    if isinstance(directory, str) and directory:
        home = str(Path.home())
        parts.append("~" + directory[len(home):] if directory == home or directory.startswith(home + "/") else directory)
    model = (status.get("model") or {}).get("display_name") if isinstance(status.get("model"), dict) else None
    if isinstance(model, str) and model:
        parts.append(f"[{model}]")
    cost = (status.get("cost") or {}).get("total_cost_usd") if isinstance(status.get("cost"), dict) else None
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        parts.append(f"${cost:.2f}")
    return " ".join(parts) + " | " if parts else ""


def read_harness_status():
    """The JSON a harness writes to the statusline's stdin, or None when stdin is a terminal or holds none."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        if os.name == "posix" and not select.select([sys.stdin], [], [], 0.5)[0]:
            return None
        return json.loads(sys.stdin.read() or "null")
    except (OSError, ValueError):
        return None


def statusline(provider, record):
    if record["status"] == "unavailable":
        return f"{provider.LABEL} unavailable"
    parts = [f"{provider.LABEL} {record['plan'] or 'no plan'}"]
    parts += [money(item) for item in record.get("balance") or []]
    for window in record["windows"]:
        part = f"{label(window)} {pct(window['used_pct'])}"
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
        for item in record.get("balance") or []:
            rows.append((name, record["plan"] or "", "balance", money(item), "", note))
        if not record["windows"] and not record.get("balance"):
            rows.append((name, record["plan"] or "", "-", "", "", note or ("no plan" if not record["plan"] else "")))
        for window in record["windows"]:
            rows.append((name, record["plan"] or "", label(window), pct(window["used_pct"]),
                         short_duration(window["resets_at"]) if window["resets_at"] else "", note))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    return "\n".join(("  ".join(cell.ljust(width) for cell, width in zip(row[:5], widths)) + "  " + row[5]).rstrip()
                     for row in rows)


def check_one(provider, record_dir=None):
    """Fetch live, compare the response's structure with the recorded shapes, and save the outcome.

    The result is error, new-shape, missing (a window the previous check saw is absent), ok, or
    expected-unavailable when the login has no quota to read, such as Gemini CLI on an API key.
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
    except ExpectedUnavailable as exc:
        state["result"] = "expected-unavailable"
        state["details"].append(str(exc))
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
        failed |= state["result"] not in ("ok", "expected-unavailable")
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
        except ExpectedUnavailable as exc:
            # Such as an account without a subscription, which an hour rarely changes.
            print(statusline(provider, unavailable(provider, account, str(exc))), flush=True)
            backoff = max(interval, 3600)
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
        print(harness_prefix(read_harness_status()) + statusline(provider, record))
    else:
        print(render(provider, record))
    return 1 if record["status"] == "unavailable" else 0


def show_all(max_age, as_json):
    """Every account of every provider, including each config directory a provider was configured with."""
    tasks = [(provider, selector) for provider in PROVIDERS.values()
             for selector in (provider.selectors() if hasattr(provider, "selectors") else [None])]

    def run(task):
        provider, selector = task
        with provider.using(selector) if selector is not None else contextlib.nullcontext():
            return provider, load(provider, max_age)

    with ThreadPoolExecutor(len(tasks)) as pool:
        results = list(pool.map(run, tasks))
    seen, rows = set(), []
    for provider, record in results:
        # Two config directories signed into one account show it once.
        key = (provider.NAME, record["account"] or id(record))
        if key not in seen:
            seen.add(key)
            rows.append((provider, record))
    if as_json:
        print(json.dumps([record for _, record in rows], indent=2, ensure_ascii=False))
    else:
        print(table(rows))
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
