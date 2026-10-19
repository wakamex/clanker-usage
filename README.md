# clanker-usage

Monitor the plan and quota of your coding-agent accounts from the terminal: current usage, a one-line statusline, a daemon that keeps a cache fresh, and a check that fails loudly when a provider changes its API. Every provider has the same commands and the same JSON document.

Providers so far: Cursor (`cursor-cli-usage`), Devin (`devin-cli-usage`) and Z.ai's Coding Plan (`zcode-cli-usage`). The other usage monitors in the same family, such as `ccusage`, `codex-cli-usage` and `agy-usage`, move into this codebase and onto the same interface, each keeping its package name and command.

clanker-usage reports what the provider says about the account: plan, quota windows and balance. Token use and cost from local session logs are the job of [clanker-analytics](https://github.com/wakamex/clanker-analytics).

```text
$ zcode-cli-usage
Z.ai: lite
  5-hour: 0% used
  weekly: 1% used, resets 2026-09-10 23:38 EDT
```

The numbers above are synthetic.

## Install

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Install the command for your provider:

```sh
uv tool install zcode-cli-usage
```

`zcode-cli-usage` installs `clanker-usage` at the same version. `zcode-cli-usage COMMAND` and `clanker-usage zcode COMMAND` are the same command.

## Commands

Every provider command takes the same subcommands:

```sh
zcode-cli-usage                   # status: the plan and each window
zcode-cli-usage json              # the usage-v1 document
zcode-cli-usage statusline        # one line for a status bar
zcode-cli-usage refresh           # fetch and update the cache; fail if unavailable
zcode-cli-usage daemon            # refresh the cache every five minutes
zcode-cli-usage check             # compare the live response with the recorded response shapes
clanker-usage                     # every account in one table; --json for every document
clanker-usage check               # check every provider
```

- `--max-age SECONDS` on `status`, `json`, `statusline` and `clanker-usage` itself reuses a cached document younger than that. It defaults to 0, always fetching, for `status` and `json`, and to 300 for `statusline` and the all-accounts table.
- `--interval SECONDS` on `daemon` sets the refresh interval. A daemon skips a refresh when another process updated the same account's cache within the interval, so several daemons or statuslines on one account do not multiply requests. After a failure it backs off, doubling up to an hour.
- When a fetch fails, the last cached document for the same account is shown as `stale`. With no cache to fall back on, the result is `unavailable`, which exits with status 1; `json` still prints the document.
- A provider can add a setup command, such as Z.ai's `configure`.

## The usage-v1 document

`json` prints one document per account, and the cache stores the same document:

```json
{
  "schema": "usage-v1",
  "provider": "zcode",
  "account": "3f2a9c1e5b7d4a60",
  "plan": "lite",
  "status": "live",
  "error": null,
  "retrieved_at": "2026-09-05T17:00:00+00:00",
  "windows": [
    {"name": "5-hour", "seconds": 18000, "scope": null, "used_pct": 0, "resets_at": null},
    {"name": "weekly", "seconds": 604800, "scope": null, "used_pct": 1, "resets_at": "2026-09-11T03:38:39.995000+00:00"}
  ],
  "details": {"limits": []}
}
```

| Field | Meaning |
| --- | --- |
| `provider` | the provider, such as `zcode` |
| `account` | a stable account id from the provider, or a one-way fingerprint of the credential when the provider gives none; null when no credential was found |
| `plan` | the plan name as the provider reports it, or null without a plan |
| `status` | `live` (fetched now), `cached` (reused under `--max-age`), `stale` (the fetch failed and this is the last cached document) or `unavailable` |
| `error` | why the fetch failed, for `stale` and `unavailable`; it never contains a credential or response body |
| `retrieved_at` | when the provider returned this data, in ISO 8601 UTC |
| `windows` | each quota window: `name`, length in `seconds` (null when the window is not a fixed length), `scope` (the models it covers, or null for the whole account), `used_pct` from 0 to 100, as exact as the provider allows, and `resets_at` in ISO 8601 UTC or null |
| `details` | what only this provider reports |

## Files

| File | Path |
| --- | --- |
| Cache | `~/.cache/clanker-usage/PROVIDER/ACCOUNT.json` |
| Check state | `~/.local/state/clanker-usage/check/PROVIDER.json` |
| Configuration | `~/.config/clanker-usage/config.json`, one section per provider |

The `XDG_CACHE_HOME`, `XDG_STATE_HOME` and `XDG_CONFIG_HOME` variables move them. Caches never contain a credential.

## Checking for provider changes

`check` fetches without the cache and reports each provider as one of:

- `ok`: the response matches a recorded shape and every window the previous check saw is present.
- `new-shape`: the response has a field or type that no recorded shape has. The new paths are listed, such as `$.data.limits[].rollover: number`.
- `missing`: a window that the previous check saw is absent.
- `error`: the request failed or the response did not parse.

It exits with status 1 unless every provider is `ok`, so it can run from a timer or CI and notify on failure. Its state file keeps paths and window names but no values. `check --record DIR` also saves a synthetic copy of each live response in `DIR`.

## Cursor

Sign in with Cursor's CLI (`agent login`) or its IDE. The tool reads the CLI's access token from `~/.config/cursor/auth.json`, and otherwise the IDE's from its `state.vscdb` database, and never refreshes or changes either. The account id is a fingerprint of the token's subject, so it stays the same when Cursor renews the token.

Usage comes from Cursor's `DashboardService/GetCurrentPeriodUsage` and the plan name from `DashboardService/GetPlanInfo`. The one window is the `billing cycle`, with the share of the plan's included usage already used; `details` keeps the spend, the limit and the separate shares for automatic model selection and for API pricing.

## Devin

Sign in with the Devin CLI. The tool reads its local API key and server from `~/.local/share/devin/credentials.toml` (under `$XDG_DATA_HOME` when set), sends requests only to that server over HTTPS, and never changes the file. The account id is a fingerprint of the key.

Usage comes from the server's `SeatManagementService/GetUserStatus`. The `daily` and `weekly` windows come from the remaining quota it reports; `details.credits` keeps the prompt, flow and flex credits and the overage balance. The tool keeps only the plan and quota parts of the response, leaving out the account's name, email and model lists.

## Z.ai Coding Plan

Export `ZAI_API_KEY`, or point the tool at an existing `.env` file once:

```sh
zcode-cli-usage configure --env-file /path/to/.env
```

Configuration stores only the file path. The tool reads `ZAI_API_KEY` from that file without executing shell code: plain or quoted assignments, optional `export`, and comments work, and shell variable expansion does not. An exported `ZAI_API_KEY` takes precedence. The account id is a fingerprint of the key, since the API names no account.

Quota data comes from `GET https://api.z.ai/api/monitor/usage/quota/limit` with `Authorization: Bearer <ZAI_API_KEY>`. Each quota becomes a window: unit `3` is hours and `6` is weeks, so the current quotas are `5-hour` and `weekly`. `used_pct` is computed from the used and total credits, since the API's own percentage is rounded; `details.limits` keeps each quota's `type`, `unit`, `number`, `used`, `limit` and `remaining` credits. An account without an active Coding Plan has a null `plan` and no windows.

## Background refresh on Linux

Save this as `~/.config/systemd/user/zcode-cli-usage.service`, adjusting the executable path if needed:

```ini
[Unit]
Description=Z.ai Coding Plan quota monitor
After=network-online.target

[Service]
ExecStart=%h/.local/bin/zcode-cli-usage daemon
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now zcode-cli-usage.service
```

## Reliability and privacy

- Requests never follow redirects, so a credential is only sent to the provider's own URL. Responses over 1 MiB are rejected, and errors name only the HTTP status or failure kind.
- Caches, state and configuration are written through a private temporary file and atomically replaced, with mode `0600` in directories the tool creates with mode `0700`.

## Development

The provider packages, such as `zcode-cli-usage`, are generated by `build_wrappers.py`. Each depends on the same version of `clanker-usage` and contains only a module named after itself that runs its provider, so its command and `python -m` module work.

A provider module in `src/clanker_usage/providers/` supplies its account id, a fetch of the raw response, and a `normalize` that turns the response into the document's `plan`, `windows` and `details`; the core does everything else. Recorded response shapes live in `src/clanker_usage/shapes/<provider>/`. Every value in them is replaced by a fixed one of the same type, except under keys that describe the format, such as a window's unit, which each provider lists in `KEEP`. Every provider is tested against every recorded shape. When `check` reports a new shape with the same meaning, save it with `check --record DIR` and add the file to that folder; when the meaning changed, fix the provider.

```sh
uv run --locked python -m unittest discover -s tests
uv run --locked python build_wrappers.py   # writes dist-wrappers/
```

`tests/test_install.py` builds the core and every provider package, installs each provider package alone with uv, and runs its command and module.

## License

MIT
