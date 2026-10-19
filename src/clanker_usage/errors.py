"""Errors a provider raises that the core treats specially."""


class ExpectedUnavailable(RuntimeError):
    """The account exists but its login has no quota to read, such as Gemini CLI on an API key.

    The document is unavailable as usual, but check reports expected-unavailable instead of an error.
    """
