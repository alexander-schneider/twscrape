"""Keep proxy credentials out of error texts, exception chains and logs.

httpx and libcurl quote the proxy URL, credentials included, in some error messages, and no redaction rule
recognises every malformed proxy value (percent-encoded or full-width separators, host:port:user:password, e-mail
user names). So with a proxy configured, a transport error keeps only its type: `proxy_safe_error` builds a new error
without the original message, cause or context, and `raise_without_context` raises it.
"""

import os
from typing import NoReturn


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


PROXY_REQUEST_FAILED = "proxy request failed; details withheld because a proxy is configured"
PROXY_SETUP_FAILED = "proxy setup failed; details withheld because a proxy is configured"


def proxy_safe_error(
    error: BaseException, proxy: str | None, *, setup: bool = False
) -> BaseException | None:
    """With a proxy configured, a new error that carries only the original's type, never its message, cause or
    context; None without a proxy, where no credential can be in it.

    No redaction rule recognises every malformed proxy value (percent-encoded or full-width separators,
    host:port:user:password, e-mail user names), so the message is dropped, not cleaned. The type is kept where it
    can be built from a message, so callers keep handling it the same way.
    """
    if not proxy_in_use(proxy):
        return None
    message = f"{PROXY_SETUP_FAILED if setup else PROXY_REQUEST_FAILED} ({type(error).__name__})"
    try:
        safe = type(error)(message)
        if str(safe) != message:
            raise TypeError("message not kept")
    except Exception:
        import httpx

        safe = ValueError(message) if setup else httpx.TransportError(message)
    request = getattr(error, "_request", None)
    if request is not None:
        setattr(safe, "_request", request)
    return safe


# The proxy variables curl-cffi (and libcurl) read from the environment when no proxy is passed.
ENV_PROXY_VARIABLES = (
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "HTTP_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


def proxy_in_use(proxy: str | None) -> bool:
    """A proxy is configured, or one the HTTP client may take from the environment."""
    return bool(proxy) or any(os.environ.get(name) for name in ENV_PROXY_VARIABLES)
