# clanker-usage

Monitor the plan and quota of your coding-agent accounts from the terminal: current usage, a one-line statusline, a daemon that keeps a cache fresh, and a check that fails loudly when a provider changes its API.

Z.ai's Coding Plan is the first provider. The other usage monitors in the same family, such as `ccusage`, `codex-cli-usage` and `agy-usage`, move into this codebase one release at a time, each keeping its own package name and command.

```text
$ zcode-cli-usage
Plan: lite
5-hour: 0% (0/2000 credits)
Weekly: 1% (50/10000 credits) resets 2026-09-10 23:38 EDT
```

The numbers above are synthetic.

## Install

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Install the command for your provider:

```sh
uv tool install zcode-cli-usage
```

`zcode-cli-usage` installs `clanker-usage` at the same version, and both commands are then available. `clanker-usage zcode ...` and `zcode-cli-usage ...` take the same arguments.

## Commands

```sh
zcode-cli-usage              # fetch and show current quotas
zcode-cli-usage status       # the same
zcode-cli-usage json         # normalized JSON with a usage-v1 summary
zcode-cli-usage refresh      # fetch and update the cache; fail if unavailable
zcode-cli-usage daemon       # refresh every five minutes, backing off after errors
zcode-cli-usage statusline   # one line; reuse a cache less than five minutes old
zcode-cli-usage check        # compare the live response with the recorded response shapes
zcode-cli-usage --version
clanker-usage check          # check every provider
python -m zcode_cli_usage --help
```

Status, JSON and statusline output fall back to the cached result when a request fails. Text output labels such a result as cached; JSON adds `"stale": true` and keeps the original `updated_at`. Reset times display in your local timezone.

### JSON output

`json` prints the provider's normalized record and adds `summary`, a usage-v1 summary that reads the same way for every provider:

```json
{
  "schema": "usage-v1",
  "account": "zcode",
  "plan": "lite",
  "status": "live",
  "error": null,
  "retrieved_at": "2026-09-05T17:00:00+00:00",
  "windows": [
    {"name": "5-hour", "used_pct": 0, "resets_at": null},
    {"name": "weekly", "used_pct": 1, "resets_at": "2026-09-11T03:38:39.995000+00:00"}
  ]
}
```

`used_pct` is the share of the window already used, from 0 to 100, and `resets_at` is ISO 8601 UTC or null. `status` is `live` for a fresh fetch, `cached` for a cache reused by `statusline`, and `stale` when a fetch failed and the cache was used. The summary has its own key because the provider records already use top-level `plan` and `status` with other meanings, such as Z.ai's `"status": "no_plan"`.

### Checking for provider changes

`check` fetches without the cache and reports each provider as one of:

- `ok`: the response matches a recorded shape and every window the previous check saw is present.
- `new-shape`: the response has a field or type that no recorded shape has. The new paths are listed, such as `$.data.limits[].rollover: number`.
- `missing`: a window that the previous check saw is absent.
- `error`: the request failed or the response did not parse.

It exits with status 1 unless every provider is `ok`, so it can run from a timer or CI and notify on failure. It saves its last result in `~/.local/state/clanker-usage/check/` (under `$XDG_STATE_HOME` when set), with paths and window names but no values. `check --record DIR` also saves a synthetic copy of each live response in `DIR`.

## Z.ai Coding Plan

Export `ZAI_API_KEY`, or point the tool at an existing `.env` file:

```sh
zcode-cli-usage configure --env-file /path/to/.env
```

Configuration stores only the file path, in `~/.config/zcode-cli-usage/config.json`. The tool reads `ZAI_API_KEY` from that file without executing shell code: plain or quoted assignments, optional `export`, and comments work, and shell variable expansion does not. An exported `ZAI_API_KEY` takes precedence, and `ZCODE_USAGE_ENV_FILE` overrides the configured path. `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` are honored, and the cache is `~/.cache/zcode-cli-usage/usage.json`.

Quota data comes from `GET https://api.z.ai/api/monitor/usage/quota/limit` with `Authorization: Bearer <ZAI_API_KEY>`. The normalized record maps `currentValue` to `used`, `usage` to `limit`, `percentage` to `pct` and `nextResetTime` from Unix milliseconds to `resets_at`, and keeps each quota's `type`, `unit`, `number` and `remaining`. The API uses unit `3` for hours and `6` for weeks. The API's percentages and remaining balances are kept as reported, because its rounded credit values can differ from arithmetic on the displayed totals. An account without an active Coding Plan is recorded as `"status": "no_plan"` with an empty `limits` list.

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

- Requests never follow redirects, so a credential is only sent to the provider's own URL. Responses over 1 MiB are rejected, and error messages name only the HTTP status or failure kind, never a response body or credential.
- Caches and state files are written through a private temporary file and atomically replaced, with mode `0600` in directories the tool creates with mode `0700`.
- The cache never contains a credential.

## Development

The provider packages, such as `zcode-cli-usage`, are generated by `build_wrappers.py`. Each depends on the same version of `clanker-usage` and contains only a module named after itself that runs its provider, so its command and `python -m` module keep working.

Recorded response shapes live in `src/clanker_usage/shapes/<provider>/`. Every value in them is replaced by a fixed one of the same type, except under keys that describe the format, such as a window's unit, which each provider lists in `KEEP`. Every provider is tested against every recorded shape. When `check` reports a new shape with the same meaning, save it with `check --record DIR` and add the file to that folder; when the meaning changed, fix the provider.

```sh
uv run --locked python -m unittest discover -s tests
uv run --locked python build_wrappers.py   # writes dist-wrappers/
```

`tests/test_install.py` builds the core and every provider package, installs each provider package alone with uv, and runs its command and module.

## License

MIT
