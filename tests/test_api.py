import os
from pathlib import Path
from unittest.mock import patch

import clanker_usage
from clanker_usage import AccountUnknown, cache, cli, paths
from clanker_usage.providers import claude, codex
from test_zcode import Home


class ReadTests(Home):
    def write(self, provider, account, **fields):
        record = {"schema": "usage-v1", "provider": provider, "account": account, "plan": "p", "status": "live",
                  "error": None, "retrieved_at": "2026-10-29T20:00:00+00:00", "windows": [], "balance": None,
                  "details": {}, **fields}
        cache.write_json(paths.cache_file(provider, account), record)
        return record

    def test_read_returns_the_document_or_none(self):
        record = self.write("codex", "a1")
        self.assertEqual(clanker_usage.read("codex", "a1"), record)
        self.assertIsNone(clanker_usage.read("codex", "missing"))

    def test_an_unavailable_reading_is_returned_not_none(self):
        self.write("claude", "a2", status="unavailable", error="no subscription")
        self.assertEqual(clanker_usage.read("claude", "a2")["status"], "unavailable")

    def test_unusable_files_read_as_none(self):
        record = self.write("codex", "someone-else")
        cache.write_json(paths.cache_file("codex", "other-account-in-file"), record)
        self.assertIsNone(clanker_usage.read("codex", "other-account-in-file"))
        path = paths.cache_file("codex", "garbled")
        path.write_text("{not json")
        self.assertIsNone(clanker_usage.read("codex", "garbled"))
        self.write("codex", "old", schema="v0")
        self.assertIsNone(clanker_usage.read("codex", "old"))
        with self.assertRaises(ValueError):
            clanker_usage.read("nonsense", "a")

    def test_documents_lists_every_account(self):
        self.write("claude", "b")
        self.write("claude", "a")
        self.assertEqual([d["account"] for d in clanker_usage.documents("claude")], ["a", "b"])
        self.assertEqual(clanker_usage.documents("devin"), [])


class AccountTests(Home):
    def test_claude_config_directories(self):
        home = Path(os.environ["HOME"])
        cache.write_json(home / ".claude.json", {"oauthAccount": {"accountUuid": "uuid-1"}})
        (home / ".claude").mkdir()
        other = Path(self.temp.name) / "aop" / "claude" / "home" / ".claude"
        cache.write_json(other / ".claude.json", {"oauthAccount": {"accountUuid": "uuid-2"}})
        # The default ~/.claude keeps its .claude.json beside it, as Claude Code does.
        self.assertEqual(clanker_usage.account("claude", home / ".claude"), claude.account())
        self.assertNotEqual(clanker_usage.account("claude", other), clanker_usage.account("claude", home / ".claude"))
        with claude.using(str(other)):
            self.assertEqual(claude.account(), clanker_usage.account("claude", other))

    def test_codex_and_agy_homes(self):
        codex_home = Path(self.temp.name) / "codex-home"
        cache.write_json(codex_home / "auth.json", {"tokens": {"access_token": "t", "account_id": "acct"}})
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            self.assertEqual(clanker_usage.account("codex", codex_home), codex.account())
        agy_home = Path(self.temp.name) / "agy-home"
        cache.write_json(agy_home / "antigravity-oauth-token", {"id_token_email": "a@example.com", "token": {"refresh_token": "r"}})
        self.assertEqual(len(clanker_usage.account("agy", agy_home)), 16)

    def test_unknown_accounts_are_reported_not_guessed(self):
        empty = Path(self.temp.name) / "empty"
        for provider in ("claude", "codex", "agy"):
            with self.subTest(provider), self.assertRaises(AccountUnknown):
                clanker_usage.account(provider, empty)
        # An agy token with only a refresh token names no stable account.
        old = Path(self.temp.name) / "old-agy"
        cache.write_json(old / "antigravity-oauth-token", {"auth_method": "oauth", "token": {"refresh_token": "r"}})
        with self.assertRaises(AccountUnknown):
            clanker_usage.account("agy", old)
        with self.assertRaises(ValueError):
            clanker_usage.account("zcode", empty)

    def test_account_and_read_meet(self):
        codex_home = Path(self.temp.name) / "codex-home"
        cache.write_json(codex_home / "auth.json", {"tokens": {"access_token": "t", "account_id": "acct"}})
        account = clanker_usage.account("codex", codex_home)
        record = cli.document(codex, account, "live", {"plan": "plus", "windows": [], "details": {}}, retrieved_at="2026-10-29T20:00:00+00:00")
        cache.write_json(paths.cache_file("codex", account), record)
        self.assertEqual(clanker_usage.read("codex", account)["plan"], "plus")


if __name__ == "__main__":
    import unittest
    unittest.main()
