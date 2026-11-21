"""Remove proxy credentials from error texts before they reach a log line or a caller.

libcurl and httpx put the proxy URL into some error messages (for example "Unsupported proxy syntax in
'http://user:pass@host:port'"). Every error a transport raises goes through `redact_credentials`.
"""

import re
from typing import NoReturn
from urllib.parse import unquote, urlsplit

REDACTED = "***"
# The whole message, when a proxy URL does not parse cleanly or a piece of its userinfo survives redaction.
PROXY_REJECTED = "proxy URL rejected by httpx/curl; credentials removed"
# The userinfo of any URL in a text: scheme://user[:password]@
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@'\"<>]+@")
# Where a parser may cut a userinfo apart: separators and %-escapes.
_USERINFO_CUTS = re.compile(r"[:/@]|%[0-9A-Fa-f]{2}")
MIN_FRAGMENT_LENGTH = 3


def _userinfo(proxy: str | None) -> str | None:
    if not proxy or "@" not in proxy:
        return None
    return proxy.split("://", 1)[-1].rsplit("@", 1)[0]


def proxy_secrets(proxy: str | None) -> list[str]:
    """The literal username and password of a proxy URL, also without a scheme and percent-decoded."""
    userinfo = _userinfo(proxy)
    if not userinfo:
        return []
    secrets: set[str] = set()
    for part in userinfo.split(":", 1):
        if part:
            secrets.update({part, unquote(part)})
    # Longest first, so a password that contains the username is replaced as a whole.
    return sorted((s for s in secrets if s), key=len, reverse=True)


def proxy_fragments(proxy: str | None) -> set[str]:
    """Pieces of the userinfo a parser may print on its own (e.g. httpx's "Invalid port: 'pa%40ss'")."""
    userinfo = _userinfo(proxy)
    if not userinfo:
        return set()
    pieces = _USERINFO_CUTS.split(userinfo) + _USERINFO_CUTS.split(unquote(userinfo))
    return {piece for piece in pieces if len(piece) >= MIN_FRAGMENT_LENGTH}


def proxy_parses_cleanly(proxy: str | None) -> bool:
    """True without credentials, or for scheme://user[:password]@host[:port] with a valid port and nothing in
    the userinfo that a parser would split differently (a raw "/", "@" or a second ":")."""
    userinfo = _userinfo(proxy)
    if userinfo is None or proxy is None:
        return True
    parts = urlsplit(proxy)
    try:
        parts.port
    except ValueError:
        return False
    if not parts.scheme or not parts.hostname or "@" not in parts.netloc:
        return False
    parsed_userinfo = parts.netloc.rsplit("@", 1)[0]
    return parsed_userinfo == userinfo and "@" not in userinfo and userinfo.count(":") <= 1


def redact_credentials(text: str, proxy: str | None = None) -> str:
    """`text` without the userinfo of any URL and without the proxy's username and password.

    When the proxy URL does not parse cleanly, or a userinfo fragment of 3 or more characters survives, the
    whole text becomes PROXY_REJECTED: a parser may have printed a piece no exact-substring rule catches.
    """
    clean = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    for secret in proxy_secrets(proxy):
        clean = clean.replace(secret, REDACTED)
    if _userinfo(proxy) and (
        not proxy_parses_cleanly(proxy)
        or any(fragment in clean for fragment in proxy_fragments(proxy))
    ):
        return PROXY_REJECTED
    return clean


def raise_without_context(error: BaseException) -> NoReturn:
    """Raise `error` with no __context__, whatever exception the interpreter still counts as handled.

    A raise after an except block is not enough: on Python 3.10 the exception a library handled inside an
    awaited coroutine (curl-cffi's `except CurlError`) stays in sys.exc_info() in the caller, and `raise` chains
    it as __context__, credentials included. A bare re-raise inside the except below keeps __context__ as set.
    An awaiting caller that is itself handling an exception may still get that exception attached by the
    interpreter on the way up; that is the caller's own error, not the transport's.
    """
    try:
        raise error
    except BaseException as raised:
        raised.__context__ = None
        raise


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
    way. It has no __cause__ or __context__, because the originals still carry the credentials; the caller raises
    it with `raise_without_context`, or Python sets __context__ again.
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
