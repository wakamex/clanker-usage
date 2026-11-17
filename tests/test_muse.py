import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from clanker_usage import cache, cli, paths, shapes
from clanker_usage.providers import muse
from test_zcode import Home

# Synthetic values in the format of Muse Code 1.4.12's key response on 2026-10-25.
KEY = {"is_subs_active": True, "subs_tier_name": "Muse Code Power Usage", "is_subs_upgrade_available": False,
       "subs_usage": {"tier": "1", "window": {"used_percent": 12, "window_duration_mins": 300, "resets_at": 1792898236},
                      "weekly": {"used_percent": 5, "resets_at": 1792972800}}}


class MuseTests(Home):
    def sign_in(self):
        cache.write_json(muse.auth_file(), {"schema_version": 1, "providers": {"meta": {
            "access_token": "access", "api_key": "key", "user_email": "Someone@Example.com", "subs_active": True}}})

    def test_normalize(self):
        body = muse.normalize(KEY)
        self.assertEqual(body["plan"], "Muse Code Power Usage")
        self.assertEqual([(w["name"], w["seconds"], w["used_pct"]) for w in body["windows"]],
                         [("5-hour", 18000, 12), ("weekly", 604800, 5)])
        self.assertEqual(body["windows"][1]["resets_at"], "2026-10-26T00:00:00+00:00")

    def test_no_subscription_and_invalid_usage(self):
        self.assertEqual(muse.normalize({"is_subs_active": False})["windows"], [])
        for change in (lambda k: k.pop("subs_usage"), lambda k: k["subs_usage"].update(window=None, weekly=None),
                       lambda k: k["subs_usage"]["weekly"].update(used_percent=-1),
                       lambda k: k["subs_usage"]["window"].update(window_duration_mins=0),
                       lambda k: k["subs_usage"]["window"].update(resets_at="soon")):
            key = copy.deepcopy(KEY)
            change(key)
            with self.assertRaises(ValueError):
                muse.normalize(key)

    def test_a_used_up_window_reads_100_and_keeps_the_reported_share(self):
        key = copy.deepcopy(KEY)
        key["subs_usage"]["window"]["used_percent"] = 101
        body = muse.normalize(key)
        self.assertEqual([w["used_pct"] for w in body["windows"]], [100, 5])
        self.assertEqual(body["details"]["reported_used_percent"], {"5-hour": 101})

    def test_fetch_sends_the_access_token_and_keeps_no_key_or_names(self):
        self.sign_in()
        raw = {**KEY, "api_key": "key", "user_email": "someone@example.com", "user_full_name": "Someone", "payment_method": None}
        with patch.object(muse.http, "request_json", return_value=raw) as request:
            self.assertEqual(muse.fetch(), KEY)
        url, headers, body = request.call_args.args
        self.assertEqual((url, headers["Authorization"], body), (muse.KEY_URL, "Bearer access", {"onboard": False}))

    def test_the_account_is_the_email_and_unsigned_is_unavailable(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run("muse-code", ["json"]), 1)
        self.assertIn("not signed in", json.loads(out.getvalue())["error"])
        self.sign_in()
        first = muse.account()
        cache.write_json(muse.auth_file(), {"providers": {"meta": {"access_token": "a2", "api_key": "k2", "user_email": "someone@example.com"}}})
        self.assertEqual(muse.account(), first)

    def test_the_example_matches_the_recorded_shape(self):
        self.assertLessEqual(shapes.paths(KEY), shapes.known_paths("muse-code"))


if __name__ == "__main__":
    import unittest
    unittest.main()


# The last line of `muse exec --json` output as aop keeps it, in the format seen in a 2026-10-26 run, with synthetic values.
RUN_LINE = ('{"type":"run_usage","calls":163,"allowance":{"as_of_ms":%d,"source":"stream","tier":null,'
            '"weekly":{"resets_at_ms":1793577600000,"used_percent":34},'
            '"window":{"resets_at_ms":1793041182000,"used_percent":51}},"cancelled_calls":0}')


class RecordedRunTests(Home):
    sign_in = MuseTests.sign_in

    def write_run(self, name, as_of_ms):
        path = Path(self.temp.name) / "runs" / name / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"type":"started"}\n' + RUN_LINE % as_of_ms + "\n")
        return path

    def configure(self):
        with redirect_stdout(io.StringIO()):
            cli.run("muse-code", ["configure", "--run-glob", str(Path(self.temp.name) / "runs" / "*" / "events.jsonl")])

    def test_a_key_response_without_usage_falls_back_to_the_newest_recorded_run(self):
        self.sign_in()
        self.write_run("old", 1793000000000)
        self.write_run("new", 1793027881478)
        self.configure()
        key = {k: v for k, v in KEY.items() if k != "subs_usage"}
        with patch.object(muse.http, "request_json", return_value=key):
            payload = muse.fetch()
        self.assertEqual(payload["run_allowance"]["as_of_ms"], 1793027881478)
        with patch.object(muse, "fetch", return_value=payload), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run("muse-code", ["json"]), 0)
        document = json.loads(out.getvalue())
        self.assertEqual(document["retrieved_at"], "2026-10-26T15:18:01+00:00")
        self.assertEqual([(w["name"], w["seconds"], w["used_pct"]) for w in document["windows"]],
                         [("5-hour", 18000, 51), ("weekly", 604800, 34)])
        self.assertEqual(document["details"]["source"], "recorded run")

    def test_a_recorded_run_never_replaces_a_newer_reading(self):
        self.sign_in()
        with patch.object(muse, "fetch", return_value=KEY), redirect_stdout(io.StringIO()):
            cli.run("muse-code", ["refresh"])
        self.write_run("old", 1793000000000)
        self.configure()
        key = {k: v for k, v in KEY.items() if k != "subs_usage"}
        with patch.object(muse.http, "request_json", return_value=key), redirect_stdout(io.StringIO()) as out:
            cli.run("muse-code", ["json"])
        document = json.loads(out.getvalue())
        self.assertNotEqual(document["details"].get("source"), "recorded run")

    def test_without_runs_a_missing_usage_is_a_failed_fetch(self):
        key = {k: v for k, v in KEY.items() if k != "subs_usage"}
        with self.assertRaisesRegex(ValueError, "no recorded Muse run"):
            muse.normalize(key)


class NoOpenWindowTests(Home):
    """A key response without usage after the last known short window reset carries the reading forward."""
    sign_in = MuseTests.sign_in

    def cache_reading(self, window_reset, weekly_reset):
        cache.write_json(paths.cache_file("muse-code", muse.account()), {
            "schema": cli.SCHEMA, "provider": "muse-code", "account": muse.account(), "plan": "Muse Code Power Usage",
            "status": "live", "error": None, "retrieved_at": "2026-11-08T04:56:06+00:00", "balance": None, "details": {},
            "windows": [{"name": "5-hour", "seconds": 18000, "scope": None, "used_pct": 83, "resets_at": window_reset},
                        {"name": "weekly", "seconds": 604800, "scope": None, "used_pct": 27, "resets_at": weekly_reset}]})

    def read(self):
        key = {k: v for k, v in KEY.items() if k != "subs_usage"}
        with patch.object(muse.http, "request_json", return_value=key), redirect_stdout(io.StringIO()) as out:
            cli.run("muse-code", ["json"])
        return json.loads(out.getvalue())

    def test_a_closed_window_reads_0_and_the_weekly_keeps_its_reading(self):
        self.sign_in()
        self.cache_reading("2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
        document = self.read()
        self.assertEqual(document["status"], "live")
        self.assertEqual([(w["name"], w["used_pct"], w["resets_at"]) for w in document["windows"]],
                         [("5-hour", 0, None), ("weekly", 27, "2999-01-01T00:00:00+00:00")])
        self.assertEqual(document["details"]["source"], "no open window")
        self.assertEqual(document["details"]["carried_from"], "2026-11-08T04:56:06+00:00")
        self.assertEqual(self.read()["details"]["carried_from"], "2026-11-08T04:56:06+00:00")

    def test_a_weekly_limit_past_its_reset_reads_0(self):
        self.sign_in()
        self.cache_reading("2000-01-01T00:00:00+00:00", "2000-01-02T00:00:00+00:00")
        self.assertEqual([w["used_pct"] for w in self.read()["windows"]], [0, 0])

    def test_missing_usage_while_the_window_is_open_is_a_failed_fetch(self):
        self.sign_in()
        self.cache_reading("2999-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
        document = self.read()
        self.assertEqual(document["status"], "stale")
        self.assertIn("window is open", document["error"])

    def test_check_compares_only_the_response(self):
        self.sign_in()
        self.cache_reading("2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
        key = {k: v for k, v in KEY.items() if k != "subs_usage"}
        with patch.object(muse.http, "request_json", return_value=key):
            self.assertEqual(cli.check_one(cli.PROVIDERS["muse-code"])["result"], "ok")
