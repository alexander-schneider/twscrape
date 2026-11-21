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


def _chain(error: BaseException) -> list[BaseException]:
    """The error and everything chained to it: __cause__, __context__ and exception group members."""
    found: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if any(current is seen for seen in found):
            continue
        found.append(current)
        pending += [e for e in (current.__cause__, current.__context__) if e is not None]
        pending += list(getattr(current, "exceptions", ()))
    return found


def _carries_credentials(error: BaseException, proxy: str | None) -> bool:
    return any(
        redact_credentials(text, proxy) != text
        for item in _chain(error)
        for text in (str(item), repr(item))
    )


def redacted_error(error: BaseException, proxy: str | None) -> BaseException | None:
    """A copy of `error` without credentials, or None when neither it nor its chain carries any.

    The copy keeps the exception type where it can be built from a message, so callers keep handling it the same
    way. It has no __cause__ or __context__, because the originals still carry the credentials; the caller must
    raise it outside an except block, or Python sets __context__ again.
    """
    if not _carries_credentials(error, proxy):
        return None
    clean = redact_credentials(str(error), proxy)
    try:
        copy = type(error)(clean)
    except Exception:
        import httpx

        copy = httpx.TransportError(clean)
    request = getattr(error, "_request", None)
    if request is not None:
        setattr(copy, "_request", request)
    return copy
