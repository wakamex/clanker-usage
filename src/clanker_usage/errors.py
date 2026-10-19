"""Errors a provider raises that the core treats specially."""


class ExpectedUnavailable(RuntimeError):
    """The account, or the login in use, has no quota to read: Gemini CLI on an API key, or a Claude
    account without a subscription.

    The document is unavailable and, once the account is known, cached as such, replacing its old
    windows. check reports expected-unavailable instead of an error.
    """
