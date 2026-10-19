import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from clanker_usage import cli, paths, shapes
from clanker_usage.providers import zcode

# Synthetic values in the response format observed with API-key authentication on 2026-09-05.
RESPONSE = {
    "code": 200, "success": True, "msg": "Operation successful",
    "data": {"level": "lite", "limits": [
        {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 2000,
         "currentValue": 0, "remaining": 2000, "percentage": 0},
        {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 10000,
         "currentValue": 50, "remaining": 9950, "percentage": 1,
         "nextResetTime": 1789097919995},
    ]},
}


class Home(unittest.TestCase):
    """A scratch home: every XDG directory in a temporary folder, and a synthetic API key."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        # Only PATH survives, so no real credential variable can reach a provider.
        env = patch.dict(os.environ, {"PATH": os.environ.get("PATH", ""), "HOME": str(root / "home"),
                                      "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
                                      "XDG_STATE_HOME": str(root / "state"), "XDG_DATA_HOME": str(root / "data"),
                                      "ZAI_API_KEY": "synthetic-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def run_command(self, *argv, response=RESPONSE, error=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(zcode, "fetch", return_value=response, side_effect=error), \
                redirect_stdout(stdout), patch("sys.stderr", stderr):
            status = cli.run("zcode", list(argv))
        return status, stdout.getvalue(), stderr.getvalue()


class ZcodeTests(Home):
    def test_every_recorded_shape_normalizes(self):
        recorded = shapes.recorded("zcode")
        self.assertEqual(set(recorded), {"no-plan-2026-10-18.json", "quota-2026-09-05.json"})
        for name, response in recorded.items():
            with self.subTest(name):
                body = zcode.normalize(response)
                cli.render(zcode, cli.document(zcode, "a", "live", body))

    def test_response(self):
        body = zcode.normalize(RESPONSE)
        self.assertEqual(body["plan"], "lite")
        self.assertEqual(body["windows"], [
            {"name": "5-hour", "seconds": 18000, "scope": None, "used_pct": 0, "resets_at": None},
            {"name": "weekly", "seconds": 604800, "scope": None, "used_pct": 0.5,
             "resets_at": "2026-09-11T03:38:39.995000+00:00"},
        ])
        self.assertEqual(body["details"]["limits"][1],
                         {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "used": 50, "limit": 10000, "remaining": 9950})

    def test_invalid_responses(self):
        for response in ({}, {"code": 401, "success": False}, {"code": 200, "success": True, "data": {"limits": []}}):
            with self.assertRaises(ValueError):
                zcode.normalize(response)
        for value in (True, -1, float("nan"), "43", None):
            response = copy.deepcopy(RESPONSE)
            response["data"]["limits"][0]["currentValue"] = value
            with self.assertRaises(ValueError):
                zcode.normalize(response)

    def test_account_without_plan(self):
        body = zcode.normalize(shapes.recorded("zcode")["no-plan-2026-10-18.json"])
        self.assertEqual((body["plan"], body["windows"]), (None, []))
        self.assertEqual(cli.render(zcode, cli.document(zcode, "a", "live", body)), "Z.ai: no plan")

    def test_unknown_quota_keeps_its_type(self):
        response = copy.deepcopy(RESPONSE)
        response["data"]["limits"][0].update(type="FUTURE_LIMIT", unit=42)
        window = zcode.normalize(response)["windows"][0]
        self.assertIn("FUTURE_LIMIT", window["name"])
        self.assertIsNone(window["seconds"])

    def test_env_file_is_not_executed(self):
        path = Path(self.temp.name) / ".env"
        path.write_text('UNRELATED=ignored\nexport ZAI_API_KEY="literal$(false)" # comment\n')
        with patch.dict(os.environ, {"ZAI_API_KEY": ""}):
            self.assertEqual(zcode.get_api_key(path), "literal$(false)")
        self.assertEqual(zcode.get_api_key(), "synthetic-key")
        self.assertEqual(zcode.get_api_key(path), "literal$(false)")

    def test_configure_stores_only_the_path_in_the_shared_config(self):
        path = Path(self.temp.name) / ".env"
        path.write_text("ZAI_API_KEY=from-file\n")
        status, out, _ = self.run_command("configure", "--env-file", str(path))
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(paths.config_file().read_text()), {"zcode": {"env_file": str(path.resolve())}})
        with patch.dict(os.environ, {"ZAI_API_KEY": ""}):
            self.assertEqual(zcode.get_api_key(), "from-file")

    def test_the_cache_is_per_account(self):
        self.run_command("refresh")
        first = paths.cache_file("zcode", zcode.account())
        self.assertTrue(first.exists())
        with patch.dict(os.environ, {"ZAI_API_KEY": "another-key"}):
            self.assertNotEqual(paths.cache_file("zcode", zcode.account()), first)
            status, out, _ = self.run_command("json", "--max-age", "300", error=RuntimeError("HTTP 500"))
        self.assertEqual((status, json.loads(out)["status"]), (1, "unavailable"))
        self.assertNotIn("synthetic-key", first.read_text())


class StandardCommandTests(Home):
    def test_json_is_one_usage_v1_document(self):
        status, out, _ = self.run_command("json")
        data = json.loads(out)
        self.assertEqual(status, 0)
        self.assertEqual(set(data), {"schema", "provider", "account", "plan", "status", "error", "retrieved_at",
                                     "windows", "balance", "details"})
        self.assertEqual((data["schema"], data["provider"], data["status"]), ("usage-v1", "zcode", "live"))
        self.assertEqual(data, {**json.loads(paths.cache_file("zcode", zcode.account()).read_text()), "status": "live"})

    def test_max_age_reuses_the_cache_and_a_failure_falls_back_to_stale(self):
        self.run_command("refresh")
        with patch.object(zcode, "fetch", side_effect=AssertionError("fetched")), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run("zcode", ["json", "--max-age", "300"]), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "cached")
        status, out, _ = self.run_command("json", error=RuntimeError("Usage request failed (HTTP 500)"))
        data = json.loads(out)
        self.assertEqual((status, data["status"], data["error"]), (0, "stale", "Usage request failed (HTTP 500)"))
        self.assertEqual(len(data["windows"]), 2)

    def test_unavailable_exits_1_and_still_prints_the_document(self):
        status, out, _ = self.run_command("json", error=RuntimeError("Could not connect to the usage API"))
        data = json.loads(out)
        self.assertEqual((status, data["status"], data["windows"]), (1, "unavailable", []))
        with patch.dict(os.environ, {"ZAI_API_KEY": ""}):
            status, out, _ = self.run_command("json")
        self.assertEqual((status, json.loads(out)["account"]), (1, None))
        self.assertIn("ZAI_API_KEY", json.loads(out)["error"])

    def test_status_and_statusline_text(self):
        status, out, _ = self.run_command()
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("Z.ai: lite\n  5-hour: 0% used\n  weekly: 0.5% used, resets 2026-09-1"), out)
        status, out, _ = self.run_command("statusline")
        # The synthetic weekly reset has passed, so no time remains to show.
        self.assertEqual(out, "Z.ai lite · 5-hour 0% · weekly 0.5%\n")
        status, out, _ = self.run_command("statusline", "--max-age", "0", error=RuntimeError("down"))
        self.assertEqual(out, "Z.ai lite · 5-hour 0% · weekly 0.5% (stale)\n")

    def test_the_core_command_alone_shows_every_account(self):
        with patch.object(zcode, "fetch", return_value=RESPONSE), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main([]), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("account"))
        zai = [line for line in lines if line.startswith("Z.ai")]
        self.assertEqual(len(zai), 2)
        self.assertRegex(zai[0], rf"^Z\.ai {zcode.account()[:8]} +lite +5-hour")
        # The other providers have no credentials in this scratch home.
        self.assertTrue(any(line.startswith("Cursor") and "unavailable" in line for line in lines))
        with patch.object(zcode, "fetch", side_effect=AssertionError("fetched")), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--json"]), 0)
        statuses = {record["provider"]: record["status"] for record in json.loads(out.getvalue())}
        self.assertEqual(statuses, {"agy": "unavailable", "cursor": "unavailable", "deepseek": "unavailable",
                                    "devin": "unavailable", "gemini": "unavailable", "zcode": "cached"})

    def test_refresh_fails_without_a_key(self):
        with patch.dict(os.environ, {"ZAI_API_KEY": ""}):
            status, _, err = self.run_command("refresh")
        self.assertEqual(status, 1)
        self.assertIn("ZAI_API_KEY", err)

    def test_daemon_skips_a_refresh_another_process_just_made(self):
        self.run_command("refresh")
        with patch.object(zcode, "fetch", side_effect=AssertionError("fetched")), \
                patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt), patch.object(cli.signal, "signal"):
            with self.assertRaises(KeyboardInterrupt):
                cli.daemon(zcode, 300)

    def test_daemon_rejects_a_zero_interval(self):
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
            cli.run("zcode", ["daemon", "--interval", "0"])


if __name__ == "__main__":
    unittest.main()
