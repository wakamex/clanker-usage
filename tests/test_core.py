import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from clanker_usage import cache, cli, http, shapes
from clanker_usage.providers import zcode
from test_zcode import RESPONSE

ROOT = Path(__file__).resolve().parents[1]


class Server:
    """A local HTTP server whose handler answers every GET with handle(handler)."""

    def __init__(self, handle):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                handle(self)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def reply(status, body=b"{}", headers=()):
    def handle(handler):
        handler.send_response(status)
        for name, value in headers:
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
    return handle


class ShapeTests(unittest.TestCase):
    def test_paths_name_every_leaf_and_its_type(self):
        self.assertEqual(shapes.paths({"a": [{"b": 1}, {"b": 1.5, "c": None}], "d": {}, "e": [], "f": True}),
                         {"$.a[].b: number", "$.a[].c: null", "$.d: object", "$.e: list", "$.f: bool"})

    def test_synthetic_keeps_types_and_kept_keys_only(self):
        value = {"unit": 3, "msg": "ok", "used": 43, "pct": 1.5, "name": "me@example.com", "on": True, "x": None,
                 "list": [{"unit": 6, "used": 9}]}
        result = shapes.synthetic(value, {"unit", "msg"})
        self.assertEqual(result, {"unit": 3, "msg": "ok", "used": 1, "pct": 0.5, "name": "synthetic", "on": True,
                                  "x": None, "list": [{"unit": 6, "used": 1}]})
        self.assertEqual(shapes.paths(result), shapes.paths(value))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "usage.json"

    def test_write_is_private_and_read_rejects_bad_files(self):
        cache.write_json(self.path, {"a": 1})
        self.assertEqual(cache.read_json(self.path), {"a": 1})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        for content in (b"[1]", b"{", b" " * (cache.LIMIT + 1)):
            self.path.write_bytes(content)
            self.assertIsNone(cache.read_json(self.path))

    def test_freshness_rejects_future_and_malformed_times(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(cache.is_fresh({"updated_at": (now - timedelta(seconds=10)).isoformat()}, 300))
        self.assertFalse(cache.is_fresh({"updated_at": (now - timedelta(seconds=400)).isoformat()}, 300))
        self.assertFalse(cache.is_fresh({"updated_at": (now + timedelta(seconds=60)).isoformat()}, 300))
        self.assertFalse(cache.is_fresh({"updated_at": "yesterday"}, 300))
        self.assertFalse(cache.is_fresh({}, 300))


class HttpTests(unittest.TestCase):
    def serve(self, handle):
        server = Server(handle)
        self.addCleanup(server.close)
        return server

    def test_redirects_are_not_followed_with_credentials(self):
        target = self.serve(reply(200))
        source = self.serve(reply(302, headers=[("Location", target.url)]))
        with self.assertRaisesRegex(RuntimeError, r"^Usage request failed \(HTTP 302\)$"):
            http.get_json(source.url, {"Authorization": "Bearer secret"})
        self.assertEqual(target.seen, [])

    def test_errors_never_echo_the_body_or_the_credential(self):
        server = self.serve(reply(401, b'{"error": "bad token Bearer secret"}'))
        with self.assertRaises(RuntimeError) as caught:
            http.get_json(server.url, {"Authorization": "Bearer secret"})
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaisesRegex(RuntimeError, "control characters"):
            http.get_json(server.url, {"Authorization": "Bearer secret\r\nX: y"})
        server = self.serve(reply(200, b"not json secret"))
        with self.assertRaises(ValueError) as caught:
            http.get_json(server.url, {})
        self.assertNotIn("secret", str(caught.exception))

    def test_oversized_bodies_are_rejected(self):
        server = self.serve(reply(200, b" " * (http.LIMIT + 1)))
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            http.get_json(server.url, {})


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def check(self, response=None, error=None, **options):
        fetch = patch.object(zcode, "fetch", return_value=response, side_effect=error)
        with fetch, redirect_stdout(io.StringIO()) as out:
            status = cli.check(["zcode"], **options)
        return status, out.getvalue(), json.loads(cli.state_path(zcode).read_text())

    def test_a_recorded_shape_passes_and_saves_no_values(self):
        status, out, state = self.check(RESPONSE)
        self.assertEqual((status, out), (0, "zcode: ok\n"))
        self.assertEqual(state["windows"], ["5-hour", "weekly"])
        self.assertNotIn("10000", json.dumps(state))

    def test_a_new_field_is_a_new_shape(self):
        response = copy.deepcopy(RESPONSE)
        response["data"]["limits"][0]["rollover"] = 5
        status, out, state = self.check(response)
        self.assertEqual((status, state["result"]), (1, "new-shape"))
        self.assertIn("$.data.limits[].rollover: number", out)

    def test_a_window_seen_before_and_absent_now_is_missing(self):
        self.check(RESPONSE)
        response = copy.deepcopy(RESPONSE)
        del response["data"]["limits"][1]
        status, out, state = self.check(response)
        self.assertEqual((status, state["result"]), (1, "missing"))
        self.assertIn("missing window: weekly", out)
        self.assertEqual(self.check(response)[0], 0)

    def test_a_failed_fetch_or_parse_is_an_error(self):
        status, out, state = self.check(error=RuntimeError("Usage request failed (HTTP 500)"))
        self.assertEqual((status, state["result"]), (1, "error"))
        self.assertIn("HTTP 500", out)
        response = copy.deepcopy(RESPONSE)
        response["data"]["limits"][0]["percentage"] = "1"
        status, out, state = self.check(response)
        self.assertEqual(state["result"], "error")
        self.assertIn("$.data.limits[].percentage: string", out)

    def test_record_saves_a_synthetic_copy(self):
        folder = Path(self.temp.name) / "recorded"
        self.check(RESPONSE, record_dir=folder)
        (path,) = folder.iterdir()
        recorded = json.loads(path.read_text())
        self.assertEqual(shapes.paths(recorded), shapes.paths(RESPONSE))
        self.assertNotIn("10000", path.read_text())


class EntrypointTests(unittest.TestCase):
    def test_entrypoints_report_the_project_version(self):
        expected = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        for command in ([sys.executable, "-m", "clanker_usage"], [str(Path(sys.executable).parent / "clanker-usage")]):
            result = subprocess.run([*command, "--version"], capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), result.stderr)
            result = subprocess.run([*command, "zcode", "daemon", "--interval", "0"], capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
