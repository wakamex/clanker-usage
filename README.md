# clanker-usage

Monitor the plan and quota of your coding-agent accounts from the terminal: current usage, a one-line statusline, a daemon that keeps a cache fresh, and a check that fails loudly when a provider changes its API. Every provider has the same commands and the same JSON document.

Providers: Antigravity (`agy-usage`), Claude Code (`ccusage`), Codex (`codex-cli-usage`), Cursor (`cursor-cli-usage`), DeepSeek (`deepseek-harness-usage`), Devin (`devin-cli-usage`), Gemini CLI (`gemini-cli-usage`), OpenCode Go (`opencode-cli-usage`) and Z.ai's Coding Plan (`zcode-cli-usage`). Each package name and command is a former standalone tool, now one interface over one codebase.

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
  "balance": null,
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
| `balance` | for an account billed from a balance, one entry per currency with its `currency` and `amount`, which goes below zero when usage outruns the balance; null for accounts with quota windows |
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

A provider whose login has no quota, such as Gemini CLI on an API key, reports `expected-unavailable`. `check` exits with status 1 unless every provider is `ok` or `expected-unavailable`, so it can run from a timer or CI and notify on failure. Its state file keeps paths and window names but no values. `check --record DIR` also saves a synthetic copy of each live response in `DIR`.

## Antigravity

Sign in with the Antigravity CLI, `agy`. The tool reads its Google login from `~/.gemini/antigravity-cli/antigravity-oauth-token`. When agy has left the access token expired, the tool renews it with the OAuth client compiled into `agy` and saves it back, but only if agy has not renewed it meanwhile, so a renewal agy made is never undone. Requests carry the installed agy version, because the quota service leaves out the Gemini group for older clients. The account id is a fingerprint of the Google account in the login's id token.

Quota comes from Google's Code Assist `retrieveUserQuotaSummary`. Each quota group, such as Gemini or Claude and GPT, has a `5-hour` and a `weekly` window, with the group as the window's `scope`; the plan is the paid tier's name, such as Google AI Ultra.

## Claude Code

Sign in with Claude Code. The tool reads its login from `.credentials.json` in Claude's config directory, `~/.claude` or `$CLAUDE_CONFIG_DIR`, and the account from Claude Code's `.claude.json`. When Claude Code has left the access token expired, the tool renews it and writes the rotated refresh token back, because Anthropic rotates refresh tokens and Claude Code would otherwise be signed out; it replaces the file only if Claude Code has not renewed it meanwhile. A request during which `/login` switched accounts is discarded. The account id is a fingerprint of Claude's account UUID, so config directories signed into the same account share one cache and one daemon's refreshes.

The windows are the `5-hour` session and the `weekly` limit, plus a `weekly` window scoped to each model with its own weekly limit, such as Fable. An account without a subscription, or in an organization that turned off OAuth, is `unavailable`, which replaces its old windows in the cache and which `check` reports as expected.

As Claude Code's status line, `ccusage statusline` reads the status JSON Claude Code pipes to it and starts the line with the directory, model and session cost. In `~/.claude/settings.json`:

```json
{"statusLine": {"type": "command", "command": "ccusage statusline"}}
```

To see several accounts in `clanker-usage`, add each extra config directory once: `ccusage configure --config-dir ~/.claude-b`.

## Codex

Sign in with Codex. The tool asks Codex itself: it starts `codex app-server` and reads `account/rateLimits/read` over its JSON-RPC protocol, so Codex handles its own login and token renewal. When Codex is not installed or does not answer, the tool reads the token Codex saved in `$CODEX_HOME/auth.json` (`~/.codex` by default) and calls the usage API directly; it never renews that token, and rereads it once if Codex renewed it during the request. The account id is a fingerprint of Codex's ChatGPT account id.

Codex's own limits are the `5-hour` and `weekly` windows. Each additional limit, such as one for a single model, adds its windows with the limit's name as their `scope`, and the API's code review limit appears as a `code review` window. `details` keeps the credits balance when the account has credits.

## Cursor

Sign in with Cursor's CLI (`agent login`) or its IDE. The tool reads the CLI's access token from `~/.config/cursor/auth.json`, and otherwise the IDE's from its `state.vscdb` database, and never refreshes or changes either. The account id is a fingerprint of the token's subject, so it stays the same when Cursor renews the token.

Usage comes from Cursor's `DashboardService/GetCurrentPeriodUsage` and the plan name from `DashboardService/GetPlanInfo`. The one window is the `billing cycle`, with the share of the plan's included usage already used; `details` keeps the spend, the limit and the separate shares for automatic model selection and for API pricing.

## DeepSeek

The tool finds the API key the way DeepSeek Harness does: `DEEPSEEK_API_KEY` in the environment, then Harness's credential store `$DSH_HOME/.credentials.yaml` (`~/.dsh` by default), then a `.env` in the current directory, then `$DSH_HOME/.env`. It parses those files as Harness does, refuses a credential store readable by other users, and never changes them. When Harness routes DeepSeek to a custom endpoint or credential name, the tool will not send that key to DeepSeek; set `CLANKER_USAGE_DEEPSEEK_API_KEY` to the official account key instead. The account id is a fingerprint of the key.

The balance comes from DeepSeek's `GET /user/balance`. DeepSeek reports no quota windows or token history, so the document has a `balance` per currency and `details` keeps the granted and topped-up parts and whether the balance still allows requests. Install `deepseek-harness-usage`, or `clanker-usage[deepseek]`, since reading Harness's YAML store needs [PyYAML](https://pyyaml.org/).

## Devin

Sign in with the Devin CLI. The tool reads its local API key and server from `~/.local/share/devin/credentials.toml` (under `$XDG_DATA_HOME` when set), sends requests only to that server over HTTPS, and never changes the file. The account id is a fingerprint of the key.

Usage comes from the server's `SeatManagementService/GetUserStatus`. The `daily` and `weekly` windows come from the remaining quota it reports; `details.credits` keeps the prompt, flow and flex credits and the overage balance. The tool keeps only the plan and quota parts of the response, leaving out the account's name, email and model lists.

## Gemini CLI

Quota exists only for a Google login. The tool chooses the login as Gemini CLI does: the `GOOGLE_GENAI_USE_GCA`, `GOOGLE_GENAI_USE_VERTEXAI`, `GEMINI_API_KEY` and Cloud Shell variables, then `.gemini/settings.json` in the current directory, then `~/.gemini/settings.json` (under `$GEMINI_CLI_HOME` when set). With any other login the document is `unavailable` and `check` reports `expected-unavailable`, which does not fail it. With a Google login it reads `~/.gemini/oauth_creds.json` and renews an expired token the same way as for Antigravity, using the OAuth client in the credentials file or in the installed Gemini CLI.

Quota comes from Code Assist `retrieveUserQuota`, with one window per model, scoped to that model. `GOOGLE_CLOUD_PROJECT` selects the Code Assist project for accounts that need one, as in Gemini CLI.

## OpenCode Go

Connect OpenCode Go in OpenCode with `/connect`. The tool reads the Go key OpenCode stores in `~/.local/share/opencode/auth.json` (under `$XDG_DATA_HOME` when set) and never changes the file. The account id is a fingerprint of the key.

The limits come from the OpenCode Console's `/console/api/go/status`, the call the console's Go page makes. It is not part of the documented Console API, so `check` is what notices when its format changes. Go's limits are dollar amounts: the `5-hour` and `weekly` windows allow 20% and 50% of the `monthly` amount, and `details.spend` keeps each window's used and total dollars. The plan is the subscription product, such as `go`.

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
