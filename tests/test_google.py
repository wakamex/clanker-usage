import base64
import copy
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from clanker_usage import cache, cli, http, shapes
from clanker_usage.providers import agy, gemini, google
from test_zcode import Home

# Synthetic values in the format observed from Antigravity on 2026-10-19.
AGY = {
    "load": {"currentTier": {"id": "free-tier", "name": "Antigravity"}, "paidTier": {"id": "g1-ultra-tier", "name": "Google AI Ultra"}},
    "summary": {"groups": [
        {"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.75, "resetTime": "2026-10-25T00:00:00Z"},
            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 1, "resetTime": "2026-10-19T05:00:00Z"}]},
        {"displayName": "Claude and GPT models", "buckets": [
            {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 0.5, "resetTime": "2026-10-25T00:00:00Z"}]},
    ]},
}
# Synthetic values in the format Gemini CLI 0.56 reads.
GEMINI = {
    "load": {"currentTier": {"id": "standard-tier", "name": "Gemini Code Assist"}},
    "quota": {"buckets": [
        {"modelId": "gemini-3.5-pro", "remainingFraction": 0.9, "remainingAmount": "90", "resetTime": "2026-10-20T07:00:00Z", "tokenType": "REQUESTS"},
        {"modelId": "", "remainingFraction": 1},
    ]},
}


def id_token(subject):
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode({'sub': subject})}.signature"


class AgyTests(Home):
    def write_auth(self, access, refresh, expiry):
        cache.write_json(agy.token_file(), {"auth_method": "oauth", "id_token": id_token("user-1"),
                                            "token": {"access_token": access, "refresh_token": refresh,
                                                      "token_type": "Bearer", "expiry": expiry}})

    def test_normalize(self):
        body = agy.normalize(AGY)
        self.assertEqual(body["plan"], "Google AI Ultra (g1-ultra-tier)")
        self.assertEqual([(w["name"], w["scope"], w["seconds"], w["used_pct"]) for w in body["windows"]], [
            ("weekly", "Gemini", 604800, 25.0), ("5-hour", "Gemini", 18000, 0.0), ("weekly", "Claude and GPT", 604800, 50.0)])
        self.assertEqual(body["windows"][0]["resets_at"], "2026-10-25T00:00:00+00:00")

    def test_invalid_and_disabled_buckets(self):
        for change in (lambda p: p["summary"].update(groups={}), lambda p: p["summary"]["groups"][0].update(buckets=None),
                       lambda p: p["summary"]["groups"][0]["buckets"][0].update(remainingFraction=1.5),
                       lambda p: p["summary"]["groups"][0]["buckets"][0].update(resetTime="tomorrow")):
            payload = copy.deepcopy(AGY)
            change(payload)
            with self.assertRaises(ValueError):
                agy.normalize(payload)
        payload = copy.deepcopy(AGY)
        payload["summary"]["groups"][1]["buckets"][0]["disabled"] = True
        body = agy.normalize(payload)
        self.assertEqual((len(body["windows"]), body["details"]), (2, {"disabled": ["Claude and GPT weekly"]}))

    def test_the_account_follows_the_id_token(self):
        self.write_auth("access-1", "refresh-1", "2099-01-01T00:00:00Z")
        first = agy.account()
        self.write_auth("access-2", "refresh-2", "2099-01-01T00:00:00Z")
        self.assertEqual(agy.account(), first)

    def test_the_account_survives_rotation_when_agy_keeps_only_the_email(self):
        for refresh in ("refresh-1", "refresh-2"):
            cache.write_json(agy.token_file(), {"auth_method": "oauth", "id_token_email": "someone@example.com",
                                                "token": {"access_token": "a", "refresh_token": refresh, "expiry": "2099-01-01T00:00:00Z"}})
            ids = getattr(self, "_ids", []) + [agy.account()]
            self._ids = ids
        self.assertEqual(len(set(self._ids)), 1)

    def test_a_fresh_token_is_used_without_renewal(self):
        self.write_auth("access-1", "refresh-1", "2099-01-01T00:00:00Z")
        with patch.object(agy.oauth, "google_refresh", side_effect=AssertionError("renewed")):
            self.assertEqual(agy.access_token(), "access-1")

    def test_an_expired_token_is_renewed_and_saved(self):
        self.write_auth("old", "refresh-1", "2000-01-01T00:00:00Z")
        with patch.object(agy, "client_candidates", return_value=[("client", "secret")]), \
                patch.object(agy.oauth, "google_refresh", return_value={"access_token": "new", "expires_in": 3600}):
            self.assertEqual(agy.access_token(), "new")
        saved = json.loads(agy.token_file().read_text())
        self.assertEqual((saved["token"]["access_token"], saved["token"]["refresh_token"], saved["auth_method"]),
                         ("new", "refresh-1", "oauth"))
        self.assertEqual(agy.token_file().stat().st_mode & 0o777, 0o600)

    def test_a_renewal_agy_made_meanwhile_is_kept(self):
        self.write_auth("old", "refresh-1", "2000-01-01T00:00:00Z")

        def renewed_by_agy(*args):
            self.write_auth("agy-renewed", "refresh-2", "2099-01-01T00:00:00Z")
            return {"access_token": "ours", "expires_in": 3600}

        with patch.object(agy, "client_candidates", return_value=[("client", "secret")]), \
                patch.object(agy.oauth, "google_refresh", side_effect=renewed_by_agy):
            self.assertEqual(agy.access_token(), "agy-renewed")
        self.assertEqual(json.loads(agy.token_file().read_text())["token"]["refresh_token"], "refresh-2")

    def test_a_rejected_client_moves_on_to_the_next(self):
        self.write_auth("old", "refresh-1", "2000-01-01T00:00:00Z")
        results = [http.StatusError("Usage request failed (HTTP 400)", 400), {"access_token": "new"}]
        with patch.object(agy, "client_candidates", return_value=[("a", "x"), ("b", "y")]), \
                patch.object(agy.oauth, "google_refresh", side_effect=results) as refresh:
            self.assertEqual(agy.access_token(), "new")
        self.assertEqual([call.args[1] for call in refresh.call_args_list], ["a", "b"])

    def test_requests_carry_the_installed_agy_version(self):
        with patch.object(agy, "agy_binary", return_value=None):
            self.assertTrue(agy.user_agent().startswith("antigravity/cli/1.4.0 "))


class GeminiTests(Home):
    def write_settings(self, folder, kind):
        cache.write_json(folder / ".gemini" / "settings.json", {"security": {"auth": {"selectedType": kind}}})

    def test_normalize(self):
        body = gemini.normalize(GEMINI)
        self.assertEqual(body["plan"], "Gemini Code Assist (standard-tier)")
        self.assertEqual(body["windows"], [{"name": "gemini-3.5-pro", "seconds": None, "scope": "gemini-3.5-pro",
                                            "used_pct": 10.0, "resets_at": "2026-10-20T07:00:00+00:00"}])
        self.assertEqual(body["details"]["models"]["gemini-3.5-pro"], {"remaining": 90, "token_type": "REQUESTS"})
        for change in (lambda p: p["quota"].update(buckets=None),
                       lambda p: p["quota"]["buckets"][0].update(remainingFraction=float("nan"))):
            payload = copy.deepcopy(GEMINI)
            change(payload)
            with self.assertRaises(ValueError):
                gemini.normalize(payload)

    def test_login_type_follows_gemini_cli(self):
        home = Path(os.environ["HOME"])
        self.write_settings(home, "oauth-personal")
        self.assertEqual(gemini.auth_type(), "oauth-personal")
        project = Path(self.temp.name) / "project"
        self.write_settings(project, "gemini-api-key")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(project)
        self.assertEqual(gemini.auth_type(), "gemini-api-key")
        with patch.dict(os.environ, {"GOOGLE_GENAI_USE_GCA": "true"}):
            self.assertEqual(gemini.auth_type(), "oauth-personal")
        with patch.dict(os.environ, {"GEMINI_CLI_HOME": str(Path(self.temp.name) / "other")}):
            os.chdir(Path(self.temp.name))
            self.assertIsNone(gemini.auth_type())

    def test_an_api_key_login_is_expected_unavailable(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "synthetic"}), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.check(["gemini"]), 0)
            self.assertEqual(cli.run("gemini", ["json"]), 1)
        self.assertIn("gemini: expected-unavailable", out.getvalue())

    def test_a_renewal_gemini_cli_made_meanwhile_is_kept(self):
        self.write_settings(Path(os.environ["HOME"]), "oauth-personal")
        cache.write_json(gemini.oauth_file(), {"access_token": "old", "refresh_token": "r1", "expiry_date": 0,
                                               "client_id": "client", "client_secret": "secret"})

        def renewed_by_gemini(*args):
            cache.write_json(gemini.oauth_file(), {"access_token": "theirs", "refresh_token": "r2", "expiry_date": 9e15})
            return {"access_token": "ours", "expires_in": 3600}

        with patch.object(gemini.oauth, "google_refresh", side_effect=renewed_by_gemini):
            self.assertEqual(gemini.access_token(), "theirs")

    def test_an_expired_token_is_renewed_with_the_client_from_the_credentials(self):
        self.write_settings(Path(os.environ["HOME"]), "oauth-personal")
        cache.write_json(gemini.oauth_file(), {"access_token": "old", "refresh_token": "r1", "expiry_date": 0,
                                               "client_id": "client", "client_secret": "secret", "id_token": id_token("u")})
        with patch.object(gemini.oauth, "google_refresh", return_value={"access_token": "new", "expires_in": 60}) as refresh:
            self.assertEqual(gemini.access_token(), "new")
        self.assertEqual(refresh.call_args.args, ("r1", "client", "secret"))
        self.assertEqual(json.loads(gemini.oauth_file().read_text())["access_token"], "new")


class GeminiClientTests(Home):
    def install(self, files):
        bundle = Path(self.temp.name) / "lib" / "gemini-cli" / "bundle"
        bundle.mkdir(parents=True)
        for name, text in files.items():
            (bundle / name).write_text(text)
        (bundle / "gemini.js").chmod(0o755)
        folder = Path(self.temp.name) / "bin"
        folder.mkdir()
        (folder / "gemini").symlink_to(bundle / "gemini.js")
        return patch.dict(os.environ, {"PATH": str(folder)})

    def test_the_client_comes_from_the_bundle_of_current_releases(self):
        with self.install({"gemini.js": "import './chunk-A.js';",
                           "chunk-A.js": 'var CLOUD_SDK_CLIENT_ID = "other"; var OAUTH_CLIENT_ID = "1-abc.apps.googleusercontent.com";'
                                         ' var OAUTH_CLIENT_SECRET = "secret-1"; var OAUTH_SCOPE = [];'}):
            self.assertEqual(gemini.client({}), ("1-abc.apps.googleusercontent.com", "secret-1"))

    def test_the_client_comes_from_oauth2_js_of_older_releases(self):
        self.assertEqual(gemini.CLIENT_ID.search("const OAUTH_CLIENT_ID = '2-x.apps.googleusercontent.com';").group(2),
                         "2-x.apps.googleusercontent.com")

    def test_the_credentials_file_client_comes_first(self):
        self.assertEqual(gemini.client({"client_id": "a", "client_secret": "b"}), ("a", "b"))


class GoogleTests(Home):
    def test_tiers_with_the_same_name_have_different_plans(self):
        lite = {"paidTier": {"id": "g1-ultra-lite-tier", "name": "Google AI Ultra"}}
        full = {"paidTier": {"id": "another-ultra-tier", "name": "Google AI Ultra"}}
        self.assertEqual(google.plan(lite), "Google AI Ultra (g1-ultra-lite-tier)")
        self.assertNotEqual(google.plan(lite), google.plan(full))
        self.assertEqual(google.plan({"currentTier": {"name": "Antigravity"}}), "Antigravity")

    def test_account_ids_prefer_the_id_token_then_the_email(self):
        self.assertEqual(google.account_id(id_token("a"), fallback="r1"), google.account_id(id_token("a"), fallback="r2"))
        # A rotated refresh token keeps the id when the email is known.
        self.assertEqual(google.account_id(None, "One@Example.com", "r1"), google.account_id(None, "one@example.com", "r2"))
        self.assertNotEqual(google.account_id(None, "one@example.com"), google.account_id(None, "two@example.com"))
        self.assertNotEqual(google.account_id(None, fallback="r1"), google.account_id(None, fallback="r2"))
        with self.assertRaises(RuntimeError):
            google.account_id("not-a-jwt", None, None)

    def test_the_examples_match_the_recorded_shapes(self):
        self.assertLessEqual(shapes.paths(AGY), shapes.known_paths("agy"))
        self.assertLessEqual(shapes.paths(GEMINI), shapes.known_paths("gemini"))


if __name__ == "__main__":
    import unittest
    unittest.main()
