"""One module per account provider. Each defines the names and functions that cli.py calls."""

from . import zcode

PROVIDERS = {module.NAME: module for module in (zcode,)}
