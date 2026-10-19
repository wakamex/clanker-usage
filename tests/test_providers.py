import base64
import copy
import io
import json
import os
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from clanker_usage import cli, paths, shapes
from clanker_usage.providers import PROVIDERS, cursor, devin, zcode
from test_zcode import Home

# Synthetic values in the formats observed on 2026-10-19.
CURSOR = {
    "usage": {
        "billingCycleStart": "1790000000000", "billingCycleEnd": "1792592000000",
        "planUsage": {"totalSpend": 650, "includedSpend": 650, "remaining": 1350, "limit": 2000,
                      "autoPercentUsed": 1.5, "apiPercentUsed": 4, "totalPercentUsed": 3.25},
        "spendLimitUsage": {"limitType": "user"}, "enabled": True,
    },
    "planInfo": {"planName": "Pro", "includedUsagePeriod": "INCLUDED_USAGE_PERIOD_MONTHLY"},
}
DEVIN = {
    "planInfo": {"planName": "Pro", "billingStrategy": "BILLING_STRATEGY_QUOTA", "monthlyPromptCredits": 50000},
    "planStatus": {"planStart": "2026-10-01T00:00:00Z", "planEnd": "2026-11-01T00:00:00Z",
                   "availablePromptCredits": 50000, "dailyQuotaRemainingPercent": 87.5,
                   "weeklyQuotaRemainingPercent": 100, "overageBalanceMicros": "-25000",
                   "dailyQuotaResetAtUnix": "1792400000", "weeklyQuotaResetAtUnix": "1792900000"},
}


def jwt(claims):
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


class RecordedShapeTests(Home):
    def test_every_provider_has_shapes_and_every_shape_normalizes(self):
        for name, provider in PROVIDERS.items():
            recorded = shapes.recorded(name)
            self.assertTrue(recorded, name)
            for file, response in recorded.items():
                with self.subTest(f"{name}/{file}"):
                    body = provider.normalize(response)
                    cli.render(provider, cli.document(provider, "a", "live", body))
                    cli.statusline(provider, cli.document(provider, "a", "live", body))

    def test_the_synthetic_examples_match_the_recorded_shapes(self):
        self.assertLessEqual(shapes.paths(CURSOR), shapes.known_paths("cursor"))
        self.assertLessEqual(shapes.paths(DEVIN), shapes.known_paths("devin"))


class CursorTests(Home):
    def write_cli_auth(self, token):
        path = cursor.cli_auth_file()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"accessToken": token, "refreshToken": "unused"}))

    def test_normalize(self):
        body = cursor.normalize(CURSOR)
        self.assertEqual(body["plan"], "Pro")
        self.assertEqual(body["windows"], [{"name": "billing cycle", "seconds": 2592000, "scope": None,
                                            "used_pct": 3.25, "resets_at": "2026-10-21T14:13:20+00:00"}])
        self.assertEqual(body["details"]["limit"], 2000)
        self.assertEqual(body["details"]["spend_limit"], {"limitType": "user"})

    def test_invalid_responses(self):
        for change in (lambda r: r["usage"].pop("planUsage"), lambda r: r["usage"].update(billingCycleEnd="soon"),
                       lambda r: r["usage"]["planUsage"].update(totalPercentUsed=float("nan"))):
            response = copy.deepcopy(CURSOR)
            change(response)
            with self.assertRaises(ValueError):
                cursor.normalize(response)
        response = copy.deepcopy(CURSOR)
        response["planInfo"] = None
        self.assertIsNone(cursor.normalize(response)["plan"])

    def test_the_cli_token_comes_first_and_the_account_survives_a_new_token(self):
        self.write_cli_auth(jwt({"sub": "user|1", "exp": 1}))
        first = cursor.account()
        cursor.cli_auth_file().write_text(json.dumps({"accessToken": jwt({"sub": "user|1", "exp": 2})}))
        self.assertEqual(cursor.account(), first)
        cursor.cli_auth_file().write_text(json.dumps({"accessToken": jwt({"sub": "user|2"})}))
        self.assertNotEqual(cursor.account(), first)

    def test_the_ide_database_is_the_fallback(self):
        path = cursor.ide_state_db()
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        connection.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (jwt({"sub": "ide"}),))
        connection.commit()
        connection.close()
        self.assertEqual(cursor.access_token(), jwt({"sub": "ide"}))

    def test_no_token_is_unavailable(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run("cursor", ["json"]), 1)
        self.assertIn("not signed in", json.loads(out.getvalue())["error"])


class DevinTests(Home):
    def write_credentials(self, text):
        path = devin.credentials_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_normalize(self):
        body = devin.normalize(DEVIN)
        self.assertEqual(body["plan"], "Pro")
        self.assertEqual(body["windows"], [
            {"name": "daily", "seconds": 86400, "scope": None, "used_pct": 12.5, "resets_at": "2026-10-19T08:53:20+00:00"},
            {"name": "weekly", "seconds": 604800, "scope": None, "used_pct": 0, "resets_at": "2026-10-25T03:46:40+00:00"},
        ])
        self.assertEqual(body["details"]["credits"]["overageBalanceMicros"], -25000)
        self.assertEqual(body["details"]["billingStrategy"], "BILLING_STRATEGY_QUOTA")

    def test_invalid_responses(self):
        for change in (lambda r: r.pop("planStatus"),
                       lambda r: r["planStatus"].update(dailyQuotaRemainingPercent=140),
                       lambda r: r["planStatus"].update(weeklyQuotaResetAtUnix="later")):
            response = copy.deepcopy(DEVIN)
            change(response)
            with self.assertRaises(ValueError):
                devin.normalize(response)

    def test_credentials(self):
        self.write_credentials('windsurf_api_key = "local-key"\napi_server_url = "https://server.example/"\n')
        self.assertEqual(devin.credentials(), ("local-key", "https://server.example"))
        for server in ("http://server.example", "https://user:pass@server.example", ""):
            self.write_credentials(f'windsurf_api_key = "local-key"\napi_server_url = "{server}"\n')
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                devin.credentials()

    def test_fetch_keeps_only_plan_and_quota(self):
        self.write_credentials('windsurf_api_key = "local-key"\napi_server_url = "https://server.example"\n')
        response = {"planInfo": {**DEVIN["planInfo"], "browserEnabled": True},
                    "userStatus": {"email": "someone@example.com", "name": "Someone",
                                   "planStatus": {**DEVIN["planStatus"], "planInfo": {"planName": "Pro"}},
                                   "cascadeModelConfigData": {"clientModelConfigs": []}}}
        with patch.object(devin.http, "request_json", return_value=response) as request, \
                patch.object(devin, "devin_version", return_value="1.2.3"):
            self.assertEqual(devin.fetch(), DEVIN)
        url, headers, body = request.call_args.args
        self.assertEqual(url, "https://server.example" + devin.USER_STATUS_PATH)
        self.assertEqual(body["metadata"]["apiKey"], "local-key")


class BackoffTests(Home):
    def test_a_recent_failure_pauses_statusline_fetches_but_not_json(self):
        failing = patch.object(zcode, "fetch", side_effect=RuntimeError("Could not connect to the usage API"))
        with failing as fetch, redirect_stdout(io.StringIO()):
            cli.run("zcode", ["statusline"])
            cli.run("zcode", ["statusline"])
            self.assertEqual(fetch.call_count, 1)
            cli.run("zcode", ["json"])
            self.assertEqual(fetch.call_count, 2)
        state = json.loads(paths.backoff_file("zcode", zcode.account()).read_text())
        self.assertEqual(state["failures"], 2)
        with patch.object(zcode, "fetch", return_value={"code": 500, "success": False, "msg": "no coding plan"}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run("zcode", ["json"]), 0)
        self.assertFalse(paths.backoff_file("zcode", zcode.account()).exists())

    def test_backoff_doubles_up_to_half_an_hour(self):
        self.assertEqual([cli.backoff_seconds(n) for n in (1, 2, 3, 6, 20)], [60, 120, 240, 1800, 1800])


if __name__ == "__main__":
    import unittest
    unittest.main()
