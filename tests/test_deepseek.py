"""DeepSeek's credential lookup, carried over with its tests from deepseek-harness-usage, and its balance."""

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
from clanker_usage.providers import deepseek
from test_zcode import Home

# Synthetic values in the response format observed on 2026-10-19.
PAYLOAD = {
    "is_available": True,
    "balance_infos": [
        {"currency": "USD", "total_balance": "123.45", "granted_balance": "23.45", "topped_up_balance": "100.00"}
    ],
}


class CredentialTests(unittest.TestCase):
    def setUp(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.dsh_home = self.root / "dsh"
        self.project = self.root / "project"
        self.dsh_home.mkdir()
        self.project.mkdir()
        self.environment = patch.dict(
            os.environ,
            {"DSH_HOME": str(self.dsh_home)},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_credential(self, text: str) -> Path:
        credential = self.dsh_home / ".credentials.yaml"
        credential.write_text(text, encoding="utf-8")
        credential.chmod(0o600)
        return credential

    def test_credential_precedence_matches_harness_defaults(self):
        self.write_credential("DEEPSEEK_API_KEY: stored-key\n")
        (self.project / ".env").write_text("DEEPSEEK_API_KEY=project-key\n")
        (self.dsh_home / ".env").write_text("DEEPSEEK_API_KEY=user-key\n")

        self.assertEqual(deepseek.get_api_key(self.project), "stored-key")
        os.environ["DEEPSEEK_API_KEY"] = "inherited-key"
        self.assertEqual(deepseek.get_api_key(self.project), "inherited-key")

    def test_project_env_beats_user_env_when_managed_store_is_absent(self):
        (self.project / ".env").write_text(
            "# comment\nexport DEEPSEEK_API_KEY='project-key'\n"
        )
        (self.dsh_home / ".env").write_text("DEEPSEEK_API_KEY=user-key\n")

        self.assertEqual(deepseek.get_api_key(self.project), "project-key")

    def test_user_env_is_the_last_fallback(self):
        (self.dsh_home / ".env").write_text('DEEPSEEK_API_KEY="user-key" # note\n')

        self.assertEqual(deepseek.get_api_key(self.project), "user-key")

    def test_dotenv_parser_matches_harness_node_semantics(self):
        cases = {
            "DEEPSEEK_API_KEY=abc#comment\n": "abc",
            "DEEPSEEK_API_KEY=abc # comment\n": "abc",
            "DEEPSEEK_API_KEY=`backtick-key`\n": "backtick-key",
            'DEEPSEEK_API_KEY="double-key" trailing\n': "double-key",
            "export DEEPSEEK_API_KEY = 'single-key'\n": "single-key",
            'DEEPSEEK_API_KEY="line\\nbreak"\n': "line\nbreak",
            "DEEPSEEK_API_KEY=first\r\nDEEPSEEK_API_KEY=second\r\n": "second",
            'DEEPSEEK_API_KEY="unterminated\nOTHER=value\n': '"unterminated',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                (self.project / ".env").write_text(text, encoding="utf-8")
                self.assertEqual(
                    deepseek._parse_node_env(text).get("DEEPSEEK_API_KEY"), expected
                )

    def test_dotenv_counterexamples_resolve_the_same_key_as_harness(self):
        for text, expected in (
            ("DEEPSEEK_API_KEY=abc#comment\n", "abc"),
            ("DEEPSEEK_API_KEY=`abc`\n", "abc"),
        ):
            with self.subTest(text=text):
                (self.project / ".env").write_text(text, encoding="utf-8")
                self.assertEqual(deepseek.get_api_key(self.project), expected)

    def test_managed_yaml_supports_harness_generated_string_styles(self):
        cases = {
            "DEEPSEEK_API_KEY: plain-key\n": "plain-key",
            "DEEPSEEK_API_KEY: 'key''quote'\n": "key'quote",
            'DEEPSEEK_API_KEY: "quoted-key"\n': "quoted-key",
            "DEEPSEEK_API_KEY: |-\n  block-key\n": "block-key",
            '"DEEPSEEK_API_KEY": quoted-name\n': "quoted-name",
            "{DEEPSEEK_API_KEY: flow-key}\n": "flow-key",
            "# café\nDEEPSEEK_API_KEY: utf8-document\n": "utf8-document",
            "DEEPSEEK_API_KEY: yes\n": "yes",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.write_credential(text)
                self.assertEqual(deepseek.get_api_key(self.project), expected)

    def test_managed_yaml_rejects_the_whole_invalid_document(self):
        secret = "fixture-secret"
        cases = [
            f"DEEPSEEK_API_KEY: {secret}\nOTHER: [unterminated\n",
            f"DEEPSEEK_API_KEY: {secret}\nOTHER: 42\n",
            f"DEEPSEEK_API_KEY: {secret}\nDEEPSEEK_API_KEY: duplicate\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.write_credential(text)
                with self.assertRaises(RuntimeError) as caught:
                    deepseek.get_api_key(self.project)
                self.assertNotIn(secret, str(caught.exception))

    def test_live_reads_reread_credentials_and_never_modify_them(self):
        credential = self.write_credential("DEEPSEEK_API_KEY: first\n")
        with patch.object(deepseek.http, "request_json", return_value=PAYLOAD) as request:
            os.chdir(self.project)
            deepseek.fetch()
            credential.write_text("DEEPSEEK_API_KEY: second\n")
            deepseek.fetch()
        self.assertEqual([call.args[1]["Authorization"] for call in request.call_args_list],
                         ["Bearer first", "Bearer second"])
        self.assertEqual(request.call_args.args[0], deepseek.BALANCE_URL)
        self.assertEqual(credential.read_text(), "DEEPSEEK_API_KEY: second\n")

    def test_missing_and_invalid_credentials_fail_without_secret_values(self):
        with self.assertRaisesRegex(RuntimeError, "no DEEPSEEK_API_KEY"):
            deepseek.get_api_key(self.project)
        secret = "sk-do-not-print"
        self.write_credential(f'DEEPSEEK_API_KEY: "{secret}\n')
        with self.assertRaises(RuntimeError) as caught:
            deepseek.get_api_key(self.project)
        self.assertNotIn(secret, str(caught.exception))

    def test_store_permissions_match_harness_safety_rule(self):
        credential = self.write_credential("DEEPSEEK_API_KEY: secret\n")
        credential.chmod(0o644)

        with self.assertRaisesRegex(RuntimeError, "chmod 600"):
            deepseek.get_api_key(self.project)

    def test_key_is_trimmed_and_illegal_header_characters_are_rejected(self):
        os.environ["DEEPSEEK_API_KEY"] = "  key-with-space-around-it  "
        self.assertEqual(deepseek.get_api_key(self.project), "key-with-space-around-it")
        os.environ["DEEPSEEK_API_KEY"] = "secret\nsecond-header"
        with self.assertRaisesRegex(RuntimeError, "HTTP header"):
            deepseek.get_api_key(self.project)

    def test_known_custom_route_is_refused_before_credential_resolution(self):
        self.write_credential("DEEPSEEK_API_KEY: gateway-secret\n")
        (self.dsh_home / "settings.yaml").write_text(
            "llm-deepseek:\n  baseURL: https://gateway.example\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "custom DeepSeek endpoint"):
            deepseek.get_api_key(self.project)

        os.environ[deepseek.EXPLICIT_KEY_ENV] = "explicit-official-key"
        self.assertEqual(deepseek.get_api_key(self.project), "explicit-official-key")

    def test_blank_custom_route_configuration_also_fails_closed(self):
        self.write_credential("DEEPSEEK_API_KEY: gateway-secret\n")
        cases = ["environment", "project", "settings"]
        for source in cases:
            with self.subTest(source=source):
                os.environ.pop("DEEPSEEK_BASE_URL", None)
                (self.project / ".env").unlink(missing_ok=True)
                (self.dsh_home / "settings.yaml").unlink(missing_ok=True)
                if source == "environment":
                    os.environ["DEEPSEEK_BASE_URL"] = ""
                elif source == "project":
                    (self.project / ".env").write_text(
                        "DEEPSEEK_BASE_URL=\n", encoding="utf-8"
                    )
                else:
                    (self.dsh_home / "settings.yaml").write_text(
                        'llm-deepseek:\n  baseURL: ""\n', encoding="utf-8"
                    )

                with self.assertRaisesRegex(
                    RuntimeError, "custom DeepSeek endpoint"
                ):
                    deepseek.get_api_key(self.project)

    def test_custom_credential_reference_requires_explicit_official_key(self):
        self.write_credential("GATEWAY_KEY: gateway-secret\n")
        (self.dsh_home / "settings.yaml").write_text(
            "llm-deepseek:\n  apiKeyEnv: GATEWAY_KEY\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "custom.*reference"):
            deepseek.get_api_key(self.project)

    def test_harness_tilde_expansion_is_lexical_and_narrow(self):
        home = str(Path.home())

        self.assertEqual(deepseek._expand_harness_home("~"), home)
        self.assertEqual(deepseek._expand_harness_home("~/dsh"), os.path.join(home, "dsh"))
        self.assertEqual(
            deepseek._expand_harness_home("~\\dsh"), os.path.join(home, "dsh")
        )
        self.assertEqual(deepseek._expand_harness_home("~other/dsh"), "~other/dsh")


class Harness02CredentialTests(CredentialTests):
    """Harness 0.2's version 1 store, its precedence, and routes in profile patches."""

    def test_a_version_1_store_ranks_above_the_environment(self):
        self.write_credential("version: 1\nrefs:\n  DEEPSEEK_API_KEY: stored-key\nrecords: {}\nforeign: ignored\n")
        os.environ["DEEPSEEK_API_KEY"] = "inherited-key"
        self.assertEqual(deepseek.get_api_key(self.project), "stored-key")

    def test_without_a_store_harness_02_falls_back_to_the_environment_then_env_files(self):
        (self.dsh_home / "profiles").mkdir()
        (self.dsh_home / ".env").write_text("DEEPSEEK_API_KEY=user-key\n")
        self.assertEqual(deepseek.get_api_key(self.project), "user-key")
        os.environ["DEEPSEEK_API_KEY"] = "inherited-key"
        self.assertEqual(deepseek.get_api_key(self.project), "inherited-key")

    def test_invalid_version_1_stores_are_rejected_whole(self):
        for text in ("version: 2\nrefs: {}\n", "version: true\nrefs: {}\n", "version: '1'\nrefs: {}\n",
                     "version: 1\nrefs: [a]\n", "version: 1\nrecords: [a]\n", "version: 1\nrefs:\n  DEEPSEEK_API_KEY: 42\n",
                     "version: 1\nrefs:\n  not-a-name: key\n"):
            with self.subTest(text=text):
                self.write_credential(text)
                with self.assertRaises(RuntimeError):
                    deepseek.get_api_key(self.project)

    def write_patch(self, text):
        profile = self.dsh_home / "profiles" / "web"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "cordis.patch.yml").write_text(text)

    def test_a_profile_patch_with_a_custom_route_is_refused(self):
        self.write_credential("version: 1\nrefs:\n  DEEPSEEK_API_KEY: gateway-secret\n")
        self.write_patch("- id: llm-deepseek-api-key\n  config:\n    baseURL: https://gateway.example/anthropic\n")
        with self.assertRaisesRegex(RuntimeError, "custom DeepSeek endpoint"):
            deepseek.get_api_key(self.project)
        self.write_patch("- id: llm-deepseek-api-key\n  config:\n    apiKeyEnv: GATEWAY_KEY\n")
        with self.assertRaisesRegex(RuntimeError, "credential reference"):
            deepseek.get_api_key(self.project)
        self.write_patch("- id: llm-deepseek-api-key\n  config:\n    baseURL: https://api.deepseek.com/anthropic\n"
                         "- id: other-plugin\n  config:\n    baseURL: https://elsewhere.example\n")
        self.assertEqual(deepseek.get_api_key(self.project), "gateway-secret")
        self.write_patch("- !!js/function 'x'\n")
        with self.assertRaisesRegex(RuntimeError, "cannot be read"):
            deepseek.get_api_key(self.project)


class BalanceTests(Home):
    def test_normalize(self):
        body = deepseek.normalize(PAYLOAD)
        self.assertEqual((body["plan"], body["windows"]), ("pay per use", []))
        self.assertEqual(body["balance"], [{"currency": "USD", "amount": 123.45}])
        self.assertEqual(body["details"], {"available": True, "balances": [
            {"currency": "USD", "total": "123.45", "granted": "23.45", "topped_up": "100.00"}]})

    def test_a_negative_balance_is_valid(self):
        # An account whose usage outran its balance, as on this host on 2026-10-19.
        payload = {"is_available": False, "balance_infos": [
            {"currency": "USD", "total_balance": "-1.25", "granted_balance": "0.00", "topped_up_balance": "-1.25"}]}
        body = deepseek.normalize(payload)
        self.assertEqual(body["balance"], [{"currency": "USD", "amount": -1.25}])
        self.assertFalse(body["details"]["available"])

    def test_currencies_are_sorted_and_drift_is_rejected(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["balance_infos"].insert(0, {**payload["balance_infos"][0], "currency": "CNY"})
        payload["balance_infos"][1]["currency"] = "USD"
        self.assertEqual([item["currency"] for item in deepseek.normalize(payload)["balance"]], ["CNY", "USD"])
        for change in (lambda p: p.pop("is_available"), lambda p: p["balance_infos"].append(dict(p["balance_infos"][0])),
                       lambda p: p["balance_infos"][0].update(total_balance="1e3"),
                       lambda p: p["balance_infos"][0].update(currency="usd"),
                       lambda p: p["balance_infos"][0].update(granted_balance=12)):
            payload = copy.deepcopy(PAYLOAD)
            change(payload)
            with self.assertRaises(ValueError):
                deepseek.normalize(payload)

    def test_json_carries_the_balance(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-key", "DSH_HOME": str(Path(self.temp.name) / "dsh")}), \
                patch.object(deepseek, "fetch", return_value=PAYLOAD), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run("deepseek", ["json"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual((data["provider"], data["balance"]), ("deepseek", [{"currency": "USD", "amount": 123.45}]))
        record = cli.document(deepseek, "a", "live", deepseek.normalize(PAYLOAD))
        self.assertEqual(cli.statusline(deepseek, record), "DeepSeek pay per use · 123.45 USD")

    def test_the_example_matches_the_recorded_shape(self):
        self.assertLessEqual(shapes.paths(PAYLOAD), shapes.known_paths("deepseek"))


if __name__ == "__main__":
    unittest.main()
