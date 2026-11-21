"""Remove proxy credentials from error texts before they reach a log line or a caller.

libcurl and httpx put the proxy URL into some error messages (for example "Unsupported proxy syntax in
'http://user:pass@host:port'"). Every error a transport raises goes through `redact_credentials`.
"""

import re
from urllib.parse import unquote

REDACTED = "***"
# The userinfo of any URL in a text: scheme://user[:password]@
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@'\"<>]+@")


def proxy_secrets(proxy: str | None) -> list[str]:
    """The literal username and password of a proxy URL, also without a scheme and percent-decoded."""
    if not proxy or "@" not in proxy:
        return []
    userinfo = proxy.split("://", 1)[-1].rsplit("@", 1)[0]
    secrets: set[str] = set()
    for part in userinfo.split(":", 1):
        if part:
            secrets.update({part, unquote(part)})
    # Longest first, so a password that contains the username is replaced as a whole.
    return sorted((s for s in secrets if s), key=len, reverse=True)


def redact_credentials(text: str, proxy: str | None = None) -> str:
    text = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    for secret in proxy_secrets(proxy):
        text = text.replace(secret, REDACTED)
    return text


def redacted_error(error: BaseException, proxy: str | None) -> BaseException | None:
    """A copy of `error` without credentials, or None when its text carries none.

    The copy keeps the exception type where it can be built from a message, so callers keep handling it the same
    way; the original is not chained, because it still carries the credentials.
    """
    text = str(error)
    clean = redact_credentials(text, proxy)
    if clean == text and redact_credentials(repr(error), proxy) == repr(error):
        return None
    try:
        copy = type(error)(clean)
    except Exception:
        import httpx

        copy = httpx.TransportError(clean)
    request = getattr(error, "_request", None)
    if request is not None:
        setattr(copy, "_request", request)
    return copy
