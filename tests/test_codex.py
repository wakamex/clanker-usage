import copy
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

from clanker_usage import cache, http, shapes
from clanker_usage.providers import codex
from test_zcode import Home

# Synthetic values in the formats observed from Codex on 2026-10-19.
APP_SERVER = {"source": "app-server", "plan_type": "plus", "result": {
    "rateLimits": {"limitId": "codex", "planType": "plus", "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                   "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1792400000},
                   "secondary": {"usedPercent": 30, "windowDurationMins": 10080, "resetsAt": 1792900000}},
    "rateLimitsByLimitId": [
        {"limitId": "codex"},
        {"limitId": "gpt-spark", "limitName": "GPT Spark",
         "primary": {"usedPercent": 50, "windowDurationMins": 300, "resetsAt": 1792400000}}],
}}
RPC_RESULT = {**APP_SERVER["result"], "accountId": "someone", "rateLimitsByLimitId": {
    item["limitId"]: item for item in APP_SERVER["result"]["rateLimitsByLimitId"]}}
API = {"source": "api", "plan_type": "plus", "usage": {
    "plan_type": "pro",
    "rate_limit": {"primary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_at": 1792400000},
                   "secondary_window": {"used_percent": 30, "limit_window_seconds": 604800, "reset_at": 1792900000}},
    "additional_rate_limits": None,
    "code_review_rate_limit": {"primary_window": {"used_percent": 5, "limit_window_seconds": 604800, "reset_at": 1792900000}},
    "credits": {"has_credits": True, "unlimited": False, "balance": "10"},
}}
FAKE_CODEX = '''#!{python}
import json, sys
for line in iter(sys.stdin.readline, ""):
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
    elif message.get("method") == "account/rateLimits/read":
        print(json.dumps({{"id": 2, "result": json.loads({result!r})}}), flush=True)
'''


class CodexTests(Home):
    def write_auth(self, access="token-1"):
        cache.write_json(codex.auth_file(), {"tokens": {"access_token": access, "account_id": "account-1",
                                                        "id_token": "x.e30.y"}})

    def test_normalize_app_server(self):
        body = codex.normalize(APP_SERVER)
        self.assertEqual(body["plan"], "plus")
        self.assertEqual([(w["name"], w["seconds"], w["scope"], w["used_pct"]) for w in body["windows"]], [
            ("5-hour", 18000, None, 12), ("weekly", 604800, None, 30), ("5-hour", 18000, "GPT Spark", 50)])
        self.assertEqual(body["windows"][0]["resets_at"], "2026-10-19T08:53:20+00:00")

    def test_normalize_api(self):
        body = codex.normalize(API)
        self.assertEqual(body["plan"], "pro")
        self.assertEqual([(w["name"], w["scope"]) for w in body["windows"]],
                         [("5-hour", None), ("weekly", None), ("code review", "code review")])
        self.assertEqual(body["details"]["credits"]["balance"], "10")

    def test_invalid_windows(self):
        for change in (lambda p: p["result"].pop("rateLimits"),
                       lambda p: p["result"]["rateLimits"]["primary"].update(usedPercent=float("nan")),
                       lambda p: p["result"]["rateLimits"]["primary"].update(windowDurationMins=0),
                       lambda p: p["result"]["rateLimits"]["primary"].update(resetsAt="soon")):
            payload = copy.deepcopy(APP_SERVER)
            change(payload)
            with self.assertRaises(ValueError):
                codex.normalize(payload)

    def test_the_account_is_codex_account_id(self):
        self.write_auth()
        first = codex.account()
        self.write_auth(access="token-2")
        self.assertEqual(codex.account(), first)
        with patch.dict(os.environ, {"CODEX_HOME": str(Path(self.temp.name) / "other")}):
            with self.assertRaisesRegex(RuntimeError, "not signed in"):
                codex.account()

    def test_the_app_server_answers_over_json_rpc(self):
        folder = Path(self.temp.name) / "bin"
        folder.mkdir()
        fake = folder / "codex"
        fake.write_text(FAKE_CODEX.format(python=sys.executable, result=json.dumps(RPC_RESULT)))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        with patch.dict(os.environ, {"PATH": f"{folder}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(codex.via_app_server(), RPC_RESULT)
            self.write_auth()
            # fetch lists the limits and keeps only what normalize reads, such as no account id.
            self.assertEqual(codex.fetch()["result"], APP_SERVER["result"])
        with patch.dict(os.environ, {"PATH": str(Path(self.temp.name) / "empty")}):
            self.assertIsNone(codex.via_app_server())

    def test_the_direct_request_rereads_a_token_codex_renewed(self):
        self.write_auth("token-1")

        def renewed_by_codex(url, headers):
            if headers["Authorization"] == "Bearer token-1":
                self.write_auth("token-2")
                raise http.StatusError("Credential rejected (HTTP 401)", 401)
            return API["usage"]

        with patch.object(codex.http, "request_json", side_effect=renewed_by_codex) as request:
            self.assertEqual(codex.direct(), API["usage"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(json.loads(codex.auth_file().read_text())["tokens"]["access_token"], "token-2")

    def test_a_rejected_token_codex_did_not_renew_is_an_error(self):
        self.write_auth("token-1")
        with patch.object(codex.http, "request_json", side_effect=http.StatusError("Credential rejected (HTTP 401)", 401)):
            with self.assertRaisesRegex(RuntimeError, "401"):
                codex.direct()

    def test_the_direct_response_keeps_only_what_normalize_reads(self):
        self.write_auth()
        raw = {**API["usage"], "email": "someone@example.com", "user_id": "u", "model_usage": {"gpt-x": {"available": True}}}
        with patch.object(codex, "via_app_server", return_value=None), patch.object(codex.http, "request_json", return_value=raw):
            self.assertEqual(codex.fetch()["usage"], API["usage"])

    def test_the_examples_match_the_recorded_shapes(self):
        # A named additional limit, such as GPT Spark, has not been seen live yet.
        seen = copy.deepcopy(APP_SERVER)
        seen["result"]["rateLimitsByLimitId"] = seen["result"]["rateLimitsByLimitId"][:1]
        self.assertLessEqual(shapes.paths(seen), shapes.known_paths("codex"))
        self.assertLessEqual(shapes.paths({**API, "usage": {**API["usage"], "code_review_rate_limit": None}}),
                             shapes.known_paths("codex"))


if __name__ == "__main__":
    import unittest
    unittest.main()
