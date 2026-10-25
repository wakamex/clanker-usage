import copy
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from clanker_usage import cache, cli, shapes
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
                       lambda k: k["subs_usage"]["weekly"].update(used_percent=101),
                       lambda k: k["subs_usage"]["window"].update(window_duration_mins=0),
                       lambda k: k["subs_usage"]["window"].update(resets_at="soon")):
            key = copy.deepcopy(KEY)
            change(key)
            with self.assertRaises(ValueError):
                muse.normalize(key)

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
