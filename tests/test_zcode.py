import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from clanker_usage import cli, shapes
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


class ZcodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp.name, "XDG_CACHE_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_every_recorded_shape_normalizes(self):
        recorded = shapes.recorded("zcode")
        self.assertEqual(set(recorded), {"no-plan-2026-10-18.json", "quota-2026-09-05.json"})
        for name, response in recorded.items():
            with self.subTest(name):
                record = zcode.normalize(response)
                zcode.windows(record)
                zcode.display(record)

    def test_response(self):
        data = zcode.normalize(RESPONSE)
        short, weekly = data["limits"]
        self.assertEqual(data["plan"], "lite")
        self.assertIsNone(short["resets_at"])
        self.assertEqual((weekly["used"], weekly["limit"], weekly["pct"], weekly["remaining"]), (50, 10000, 1, 9950))
        self.assertEqual(weekly["resets_at"], "2026-09-11T03:38:39.995000+00:00")
        self.assertEqual([window["name"] for window in zcode.windows(data)], ["5-hour", "weekly"])

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
        data = zcode.normalize(shapes.recorded("zcode")["no-plan-2026-10-18.json"])
        self.assertEqual((data["status"], data["plan"], data["limits"]), ("no_plan", None, []))
        self.assertIn("updated_at", data)
        self.assertEqual(zcode.display(data), "No active Coding Plan")

    def test_unknown_quota_preserved(self):
        response = copy.deepcopy(RESPONSE)
        response["data"]["limits"][0].update(type="FUTURE_LIMIT", unit=42)
        item = zcode.normalize(response)["limits"][0]
        self.assertIn("FUTURE_LIMIT", zcode.label(item))

    def test_env_file_is_not_executed(self):
        path = Path(self.temp.name) / ".env"
        path.write_text('UNRELATED=ignored\nexport ZAI_API_KEY="literal$(false)" # comment\n')
        with patch.dict(os.environ, {"ZAI_API_KEY": ""}):
            self.assertEqual(zcode.get_api_key(path), "literal$(false)")
        with patch.dict(os.environ, {"ZAI_API_KEY": "override"}):
            self.assertEqual(zcode.get_api_key(path), "override")

    def test_cache_and_stale_fallback(self):
        with patch.object(zcode, "fetch", return_value=RESPONSE) as fetch:
            record, status = cli.load(zcode)
            self.assertEqual(status, "live")
            self.assertEqual(cli.load(zcode, cached=True), (record, "cached"))
            fetch.assert_called_once()
        with patch.object(zcode, "fetch", side_effect=RuntimeError("HTTP 401")):
            stale, status = cli.load(zcode)
            self.assertEqual(status, "stale")
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["updated_at"], record["updated_at"])
        self.assertNotIn("stale", json.loads(zcode.cache_path().read_text()))
        self.assertEqual(zcode.cache_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(zcode.cache_path().parent.stat().st_mode & 0o777, 0o700)

    def test_json_keeps_the_record_and_adds_the_summary(self):
        with patch.object(zcode, "fetch", return_value=RESPONSE), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run("zcode", ["json"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["plan"], "lite")
        self.assertEqual(len(data["limits"]), 2)
        self.assertEqual(data["summary"]["schema"], "usage-v1")
        self.assertEqual(data["summary"]["status"], "live")
        self.assertEqual(data["summary"]["windows"][1],
                         {"name": "weekly", "used_pct": 1, "resets_at": "2026-09-11T03:38:39.995000+00:00"})

    def test_configure_stores_only_the_path(self):
        path = Path(self.temp.name) / ".env"
        path.write_text("ZAI_API_KEY=secret\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run("zcode", ["configure", "--env-file", str(path)]), 0)
        self.assertEqual(json.loads(zcode.config_path().read_text()), {"env_file": str(path.resolve())})

    def test_refresh_fails_without_a_key(self):
        env = {"ZAI_API_KEY": "", "ZCODE_USAGE_ENV_FILE": str(Path(self.temp.name) / "missing.env")}
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(cli.run("zcode", ["refresh"]), 1)
        self.assertIn("ZAI_API_KEY", err.getvalue())

    def test_daemon_rejects_a_zero_interval(self):
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
            cli.run("zcode", ["daemon", "--interval", "0"])


if __name__ == "__main__":
    unittest.main()
