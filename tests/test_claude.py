import copy
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from clanker_usage import cache, cli, http, paths, shapes
from clanker_usage.errors import ExpectedUnavailable
from clanker_usage.providers import claude
from test_zcode import Home

# Synthetic values in the format observed from Anthropic on 2026-10-19.
USAGE = {"limits": [
    {"kind": "session", "percent": 22, "resets_at": "2026-10-19T05:00:00+00:00", "scope": None, "is_active": False},
    {"kind": "weekly_all", "percent": 42, "resets_at": "2026-10-23T14:00:00+00:00", "scope": None, "is_active": True},
    {"kind": "weekly_scoped", "percent": 0, "resets_at": "2026-10-23T14:00:00+00:00",
     "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": False},
], "extra_usage": {"is_enabled": False}}
PAYLOAD = {"usage": USAGE, "plan": "max_20x"}


class ClaudeHome(Home):
    def sign_in(self, directory=None, uuid="account-1", access="token-1", refresh="refresh-1", expires=9e15):
        folder = Path(directory) if directory else Path(os.environ["HOME"]) / ".claude"
        cache.write_json(Path(directory or os.environ["HOME"]) / ".claude.json", {"oauthAccount": {"accountUuid": uuid}})
        cache.write_json(folder / ".credentials.json", {"claudeAiOauth": {
            "accessToken": access, "refreshToken": refresh, "expiresAt": expires, "rateLimitTier": "default_claude_max_20x"}})
        return folder


class ClaudeTests(ClaudeHome):
    def test_normalize(self):
        body = claude.normalize(PAYLOAD)
        self.assertEqual(body["plan"], "max_20x")
        self.assertEqual([(w["name"], w["seconds"], w["scope"], w["used_pct"]) for w in body["windows"]], [
            ("5-hour", 18000, None, 22), ("weekly", 604800, None, 42), ("weekly", 604800, "Fable", 0)])
        self.assertEqual(body["details"], {})

    def test_invalid_limits(self):
        for change in (lambda p: p["usage"].update(limits={}), lambda p: p["usage"]["limits"][0].update(percent=101),
                       lambda p: p["usage"]["limits"][0].update(resets_at="2026-10-19T05:00:00"),
                       lambda p: p["usage"]["limits"][2]["scope"].update(model={})):
            payload = copy.deepcopy(PAYLOAD)
            change(payload)
            with self.assertRaises(ValueError):
                claude.normalize(payload)

    def test_the_plan_and_account_come_from_claude_code(self):
        self.sign_in()
        self.assertEqual(claude.plan(claude.read_credentials()), "max_20x")
        first = claude.account()
        other = Path(self.temp.name) / "claude-b"
        self.sign_in(other, uuid="account-1", access="token-b")
        with claude.using(str(other)):
            self.assertEqual(claude.account(), first)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other)}):
            self.assertEqual(claude.credentials_file(), other / ".credentials.json")
            self.assertEqual(claude.account(), first)

    def test_an_expired_token_is_renewed_and_the_rotated_refresh_token_saved(self):
        self.sign_in(expires=0)
        with patch.object(claude.http, "request_json", side_effect=[
                {"access_token": "token-2", "refresh_token": "refresh-2", "expires_in": 3600}, USAGE]) as request:
            payload = claude.fetch()
        self.assertEqual(payload["plan"], "max_20x")
        self.assertEqual(request.call_args_list[0].args[0], claude.TOKEN_URL)
        self.assertEqual(request.call_args_list[1].args[1]["Authorization"], "Bearer token-2")
        saved = json.loads(claude.credentials_file().read_text())["claudeAiOauth"]
        self.assertEqual((saved["accessToken"], saved["refreshToken"], saved["rateLimitTier"]),
                         ("token-2", "refresh-2", "default_claude_max_20x"))

    def test_a_rotation_claude_code_made_meanwhile_is_kept(self):
        self.sign_in(expires=0)

        def rotated_by_claude_code(url, headers, body=None):
            if url == claude.TOKEN_URL:
                self.sign_in(access="token-cc", refresh="refresh-cc")
                return {"access_token": "token-ours", "refresh_token": "refresh-ours"}
            return USAGE

        with patch.object(claude.http, "request_json", side_effect=rotated_by_claude_code):
            claude.fetch()
        saved = json.loads(claude.credentials_file().read_text())["claudeAiOauth"]
        self.assertEqual((saved["accessToken"], saved["refreshToken"]), ("token-cc", "refresh-cc"))

    def test_a_rejected_token_rereads_then_renews_once(self):
        self.sign_in()
        rejected = http.StatusError("Credential rejected (HTTP 401)", 401)
        with patch.object(claude.http, "request_json", side_effect=[
                rejected, {"access_token": "token-2", "expires_in": 3600}, USAGE]) as request:
            claude.fetch()
        self.assertEqual([call.args[0] for call in request.call_args_list], [claude.USAGE_URL, claude.TOKEN_URL, claude.USAGE_URL])

    def test_a_403_is_expected_unavailable_and_replaces_the_cached_windows(self):
        self.sign_in()
        with patch.object(claude, "fetch", return_value=PAYLOAD), redirect_stdout(io.StringIO()):
            cli.run("claude", ["refresh"])
        with patch.object(claude.http, "request_json", side_effect=http.StatusError("x", 403)):
            with self.assertRaises(ExpectedUnavailable):
                claude.fetch()
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.run("claude", ["json"]), 1)
                self.assertEqual(cli.check(["claude"]), 0)
        data = json.loads(out.getvalue().split("\nclaude:")[0])
        self.assertEqual((data["status"], data["windows"]), ("unavailable", []))
        with patch.object(claude, "fetch", side_effect=AssertionError("fetched")), redirect_stdout(io.StringIO()) as out:
            cli.run("claude", ["statusline"])
        self.assertEqual(out.getvalue(), "Claude unavailable\n")

    def test_an_account_switch_during_the_request_discards_it(self):
        self.sign_in()

        def switch(url, headers, body=None):
            cache.write_json(claude.account_file(), {"oauthAccount": {"accountUuid": "account-2"}})
            return USAGE

        with patch.object(claude.http, "request_json", side_effect=switch):
            with self.assertRaisesRegex(RuntimeError, "switched accounts"):
                claude.fetch()

    def test_configure_adds_a_config_dir_to_the_all_accounts_view(self):
        self.sign_in()
        other = Path(self.temp.name) / "claude-c"
        self.sign_in(other, uuid="account-2")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run("claude", ["configure", "--config-dir", str(other)]), 0)
        self.assertEqual(paths.config("claude")["config_dirs"], [str(other.resolve())])
        seen = []

        def fetch():
            seen.append(claude.account_uuid())
            return PAYLOAD

        with patch.object(claude, "fetch", side_effect=fetch), redirect_stdout(io.StringIO()) as out:
            cli.main(["--json"])
        accounts = [r["account"] for r in json.loads(out.getvalue()) if r["provider"] == "claude"]
        self.assertEqual(sorted(seen), ["account-1", "account-2"])
        self.assertEqual(len(set(accounts)), 2)

    def test_the_example_matches_the_recorded_shape(self):
        self.assertLessEqual(shapes.paths(PAYLOAD), shapes.known_paths("claude"))


class StatuslineTests(ClaudeHome):
    def test_claude_codes_status_json_adds_directory_model_and_cost(self):
        home = os.environ["HOME"]
        status = {"workspace": {"current_dir": f"{home}/code/usage"}, "model": {"display_name": "Opus"},
                  "cost": {"total_cost_usd": 1.5}}
        self.assertEqual(cli.harness_prefix(status), "~/code/usage [Opus] $1.50 | ")
        self.assertEqual(cli.harness_prefix(None), "")
        self.assertEqual(cli.harness_prefix({"model": "not an object"}), "")
        self.sign_in()
        with patch.object(claude, "fetch", return_value=PAYLOAD), patch.object(cli, "read_harness_status", return_value=status), \
                redirect_stdout(io.StringIO()) as out:
            cli.run("claude", ["statusline"])
        self.assertTrue(out.getvalue().startswith("~/code/usage [Opus] $1.50 | Claude max_20x · 5-hour 22%"), out.getvalue())


if __name__ == "__main__":
    import unittest
    unittest.main()
