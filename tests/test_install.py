import importlib.util
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_wrappers", ROOT / "build_wrappers.py")
build_wrappers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_wrappers)
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


@unittest.skipUnless(shutil.which("uv"), "uv is needed to build and install the packages")
class InstalledPackageTests(unittest.TestCase):
    """Build the real wheels, install each wrapper alone with uv, and run its command and module."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.wheels = root / "wheels"
        cls.build(ROOT)
        for name in build_wrappers.WRAPPERS:
            cls.build(build_wrappers.build(name, root / "wrappers"))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def build(cls, source):
        subprocess.run(["uv", "--no-config", "build", "--no-sources", "-q", "--out-dir", str(cls.wheels)],
                       cwd=source, check=True, capture_output=True)

    def test_each_wrapper_keeps_its_command_and_module(self):
        for package in build_wrappers.WRAPPERS:
            with self.subTest(package=package):
                venv = Path(self.temp.name) / f"venv-{package}"
                subprocess.run(["uv", "venv", "-q", str(venv)], check=True, capture_output=True)
                subprocess.run(["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"),
                                "--no-index", "--find-links", str(self.wheels), package], check=True, capture_output=True)
                home = Path(self.temp.name) / f"home-{package}"
                env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / "config"),
                           XDG_CACHE_HOME=str(home / "cache"), ZAI_API_KEY="")
                module = package.replace("-", "_")
                for command in ([str(venv / "bin" / package)], [str(venv / "bin" / "python"), "-m", module]):
                    result = subprocess.run([*command, "--version"], capture_output=True, text=True, env=env)
                    self.assertEqual((result.returncode, result.stdout.strip()), (0, VERSION), result.stderr)
                    result = subprocess.run([*command, "refresh"], capture_output=True, text=True, env=env)
                    self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue((venv / "bin" / "clanker-usage").exists())


if __name__ == "__main__":
    unittest.main()
