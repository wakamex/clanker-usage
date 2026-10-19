"""Generate the wrapper packages that keep each provider's original package name and command.

clanker-usage is the only codebase. A wrapper depends on the same version of clanker-usage and
contains one generated module named after itself, so `NAME` and `python -m MODULE` run that
provider. The module only calls clanker-usage's `run` for its provider.

Usage: python build_wrappers.py [OUT_DIR] [PACKAGE ...]   (default OUT_DIR: dist-wrappers)
"""

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE = "clanker-usage"
REPOSITORY = "https://github.com/wakamex/clanker-usage"

WRAPPERS = {
    "agy-usage": {
        "provider": "agy", "label": "Antigravity",
        "description": "Antigravity quota monitor",
        "keywords": ["antigravity", "agy", "gemini", "usage", "quota", "statusline", "cli"],
    },
    "cursor-cli-usage": {
        "provider": "cursor", "label": "Cursor",
        "description": "Cursor plan usage monitor",
        "keywords": ["cursor", "cursor-agent", "usage", "quota", "statusline", "cli"],
    },
    "deepseek-harness-usage": {
        "provider": "deepseek", "label": "DeepSeek", "extra": "deepseek",
        "description": "DeepSeek account balance monitor for DeepSeek Harness",
        "keywords": ["deepseek", "deepseek-harness", "balance", "usage", "statusline", "cli"],
    },
    "devin-cli-usage": {
        "provider": "devin", "label": "Devin",
        "description": "Devin quota and credits monitor",
        "keywords": ["devin", "windsurf", "usage", "quota", "statusline", "cli"],
    },
    "gemini-cli-usage": {
        "provider": "gemini", "label": "Gemini CLI",
        "description": "Gemini CLI model quota monitor",
        "keywords": ["gemini", "gemini-cli", "google", "usage", "quota", "statusline", "cli"],
    },
    "zcode-cli-usage": {
        "provider": "zcode", "label": "Z.ai Coding Plan",
        "description": "Z.ai Coding Plan quota monitor",
        "keywords": ["z.ai", "zai", "glm", "coding-plan", "usage", "quota", "statusline", "cli"],
    },
}

PYPROJECT = '''[build-system]
requires = ["hatchling==1.32.4"]
build-backend = "hatchling.build"

[project]
name = "{name}"
version = "{version}"
description = "{description}"
readme = "README.md"
keywords = [{keywords}]
requires-python = ">=3.12"
license = "MIT"
license-files = ["LICENSE"]
dependencies = ["{core}{extras}=={version}"]

[project.urls]
Homepage = "{repository}"
Source = "{repository}"

[project.scripts]
{name} = "{module}:main"

[tool.hatch.build.targets.wheel]
packages = ["{module}"]
'''

INIT = '''"""{name}: the {label} command of {core}."""

from clanker_usage import run


def main():
    return run("{provider}")
'''

MAIN = '''from . import main

raise SystemExit(main())
'''

README = '''# {name}

Monitor your {label} quota from the terminal: current usage, a one-line statusline, and a daemon that keeps a cache fresh.

```sh
uv tool install {name}
{name}              # current quotas
{name} statusline   # one line, from a cache less than five minutes old
{name} json         # the usage-v1 document
```

`{name}` is the {label} command of [{core}]({repository}), so it installs {core} at the same version. The documentation lives there.
'''


def core_version():
    match = re.search(r'(?m)^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text())
    if not match:
        raise SystemExit("no version in pyproject.toml")
    return match[1]


def build(name, out_root):
    if name not in WRAPPERS:
        raise SystemExit(f"unknown wrapper package: {name}")
    spec = WRAPPERS[name]
    values = {
        "name": name, "module": name.replace("-", "_"), "version": core_version(), "core": CORE,
        "repository": REPOSITORY, **spec,
    }
    values["keywords"] = ", ".join(f'"{word}"' for word in spec["keywords"])
    values["extras"] = f"[{spec['extra']}]" if spec.get("extra") else ""
    out = out_root / name
    if out.exists():
        shutil.rmtree(out)
    package = out / values["module"]
    package.mkdir(parents=True)
    (out / "pyproject.toml").write_text(PYPROJECT.format(**values))
    (out / "README.md").write_text(README.format(**values))
    (package / "__init__.py").write_text(INIT.format(**values))
    (package / "__main__.py").write_text(MAIN)
    shutil.copy(ROOT / "LICENSE", out / "LICENSE")
    return out


def main(argv):
    out_root = Path(argv[0]) if argv else ROOT / "dist-wrappers"
    for name in argv[1:] or WRAPPERS:
        print(build(name, out_root))


if __name__ == "__main__":
    main(sys.argv[1:])
