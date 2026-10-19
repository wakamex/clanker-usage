"""One module per account provider. Each defines the names and functions that cli.py calls:

NAME, LABEL, PACKAGE, DESCRIPTION, INTERVAL and KEEP; account(), a stable id or credential
fingerprint; fetch(), the raw response, reduced to the parts the provider reads; and normalize(),
which turns that response into the document's plan, windows, details and, for a balance account,
balance. A provider may add
add_commands(sub) and run_command(args) for its own setup command.
"""

from . import cursor, deepseek, devin, zcode

PROVIDERS = {module.NAME: module for module in (cursor, deepseek, devin, zcode)}
