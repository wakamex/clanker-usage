import copy
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from clanker_usage import cache, cli, shapes
from clanker_usage.providers import opencode
from test_zcode import Home

# Synthetic values in the format of the console's Go status on 2026-10-19.
STATUS = {"product": "go", "renewalProduct": "go", "resumability": "renewing", "cancelAtPeriodEnd": False,
          "access": {"startsAt": "2026-10-15T09:22:00Z", "endsAt": "2026-11-15T10:22:00Z", "cancelAtPeriodEnd": False,
                     "meters": {
                         "fiveHour": {"startsAt": "2026-10-19T11:26:00Z", "resetsAt": "2026-10-19T16:26:00Z",
                                      "limitMicroCents": "1200000000", "usedMicroCents": "0"},
                         "week": {"startsAt": "2026-10-19T00:00:00Z", "resetsAt": "2026-10-26T00:00:00Z",
                                  "limitMicroCents": "3000000000", "usedMicroCents": "450000000"},
                         "month": {"resetsAt": "2026-11-15T10:22:00Z", "limitMicroCents": "6000000000",
                                   "usedMicroCents": "450000000"}}}}


class OpencodeTests(Home):
    def test_normalize(self):
        body = opencode.normalize(STATUS)
        self.assertEqual(body["plan"], "go")
        self.assertEqual([(w["name"], w["seconds"], w["used_pct"]) for w in body["windows"]],
                         [("5-hour", 18000, 0), ("weekly", 604800, 15), ("monthly", None, 7.5)])
        self.assertEqual(body["windows"][1]["resets_at"], "2026-10-26T00:00:00+00:00")
        self.assertEqual(body["details"]["spend"]["weekly"], {"used_usd": 4.5, "limit_usd": 30})
        self.assertTrue(body["details"]["renews"])

    def test_no_subscription_has_no_plan(self):
        self.assertEqual(opencode.normalize({"product": None, "access": None})["windows"], [])

    def test_invalid_meters(self):
        for change in (lambda s: s["access"].update(meters=None), lambda s: s["access"].update(meters={}),
                       lambda s: s["access"]["meters"]["week"].update(usedMicroCents=-5),
                       lambda s: s["access"]["meters"]["week"].update(usedMicroCents="1e9"),
                       lambda s: s["access"]["meters"]["week"].update(resetsAt="later")):
            status = copy.deepcopy(STATUS)
            change(status)
            with self.assertRaises(ValueError):
                opencode.normalize(status)

    def test_the_key_comes_from_opencodes_auth_file(self):
        with self.assertRaisesRegex(RuntimeError, "no Go key"):
            opencode.api_key()
        cache.write_json(opencode.auth_file(), {"opencode": {"type": "api", "key": "oc_sk_zen"},
                                                "opencode-go": {"type": "api", "key": "oc_sk_go"}})
        self.assertEqual(opencode.api_key(), "oc_sk_go")

    def test_fetch_leaves_out_payment_details_and_user_ids(self):
        cache.write_json(opencode.auth_file(), {"opencode-go": {"type": "api", "key": "oc_sk_go"}})
        raw = {**STATUS, "paymentMethodId": "pm_1", "subscriberUserId": "usr_1", "useBalance": False}
        with patch.object(opencode.http, "request_json", return_value=raw) as request:
            self.assertEqual(opencode.fetch(), STATUS)
        self.assertEqual(request.call_args.args[1], {"Authorization": "Bearer oc_sk_go"})

    def test_json(self):
        cache.write_json(opencode.auth_file(), {"opencode-go": {"type": "api", "key": "oc_sk_go"}})
        with patch.object(opencode, "fetch", return_value=STATUS), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run("opencode", ["json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["provider"], "opencode")

    def test_the_example_matches_the_recorded_shape(self):
        self.assertLessEqual(shapes.paths(STATUS), shapes.known_paths("opencode"))


if __name__ == "__main__":
    import unittest
    unittest.main()
