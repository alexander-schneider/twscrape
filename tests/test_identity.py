"""What X sees of an account: its exit proxy and its client fingerprint.

Ways this can fail, covered below:
- a shared TWS_PROXY or API proxy overrides the account's own proxy, so every account leaves through one gateway;
- with impersonation on, requests still carry the account's stored user agent or httpx's own headers, so the
  user agent and the TLS fingerprint disagree;
- the impersonated client drops cookies, the csrf header, the bearer token or JSON bodies, or does not store
  cookies X sets;
- the x-client-transaction-id page fetch uses a different fingerprint than the API requests.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from twscrape.account import Account, resolve_proxy
from twscrape.xclid import _make_client

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1"
)


@pytest.mark.parametrize(
    ("account_proxy", "api_proxy", "env_proxy", "expected"),
    [
        ("http://own:1", "http://api:2", "http://env:3", "http://own:1"),
        (None, "http://api:2", "http://env:3", "http://api:2"),
        (None, None, "http://env:3", "http://env:3"),
        (None, None, None, None),
    ],
)
def test_account_proxy_wins(monkeypatch, account_proxy, api_proxy, env_proxy, expected):
    if env_proxy:
        monkeypatch.setenv("TWS_PROXY", env_proxy)
    else:
        monkeypatch.delenv("TWS_PROXY", raising=False)
    assert resolve_proxy(account_proxy, api_proxy) == expected


class _Recorder(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def _answer(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        headers = {key.lower(): value for key, value in self.headers.items()}
        _Recorder.seen.append({"method": self.command, "headers": headers, "body": body})
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("set-cookie", "ct0=rotated; Path=/")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.fixture
def server():
    _Recorder.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def _account() -> Account:
    return Account(
        username="u1",
        password="_",
        email="_",
        email_password="_",
        user_agent=IPHONE_UA,
        active=True,
        cookies={"auth_token": "tok", "ct0": "csrf"},
    )


async def test_impersonated_client_sends_one_browser_identity(server, monkeypatch):
    monkeypatch.delenv("TWS_PROXY", raising=False)
    client = _account().make_client(impersonate="chrome")
    async with client:
        rep = await client.get(f"{server}/graphql/x/SearchTimeline", params={"variables": "{}"})
        await client.post(f"{server}/graphql/x/Followers", json={"variables": {"a": 1}})

    assert rep.status_code == 200 and rep.json() == {"ok": True}
    get, post = _Recorder.seen
    ua = get["headers"]["user-agent"]
    assert "Chrome/" in ua and "iPhone" not in ua and "python-httpx" not in ua
    assert get["headers"]["x-csrf-token"] == "csrf"
    assert get["headers"]["authorization"].startswith("Bearer ")
    assert "auth_token=tok" in get["headers"]["cookie"]
    assert "sec-ch-ua" in get["headers"]  # the browser profile's own client hints
    assert json.loads(post["body"]) == {"variables": {"a": 1}}
    assert "rotated" in [cookie.value for cookie in client.cookies.jar if cookie.name == "ct0"]


async def test_without_impersonation_the_stored_user_agent_is_kept(server, monkeypatch):
    monkeypatch.delenv("TWS_PROXY", raising=False)
    monkeypatch.delenv("TWS_IMPERSONATE", raising=False)
    async with _account().make_client() as client:
        await client.get(server)
    assert _Recorder.seen[0]["headers"]["user-agent"] == IPHONE_UA


async def test_transaction_id_page_fetch_uses_the_same_fingerprint(server):
    async with _make_client(
        user_agent=IPHONE_UA, cookies={"auth_token": "tok"}, impersonate="chrome"
    ) as client:
        await client.get(server)
    ua = _Recorder.seen[0]["headers"]["user-agent"]
    assert "Chrome/" in ua and "iPhone" not in ua


async def test_api_calls_look_like_the_web_clients_fetch_not_a_page_load(server, monkeypatch):
    monkeypatch.delenv("TWS_PROXY", raising=False)
    variables = json.dumps({"rawQuery": "$NVDA lang:en", "product": "Latest"})
    async with _account().make_client(impersonate="chrome") as client:
        await client.get(
            f"{server}/i/api/graphql/x/SearchTimeline", params={"variables": variables}
        )
        await client.post(f"{server}/i/api/graphql/x/Followers", json={"variables": {}})
        await client.get(f"{server}/tesla")  # the page the transaction-id signer loads

    search, post, page = (request["headers"] for request in _Recorder.seen)
    for api in (search, post):
        assert (
            api["accept"],
            api["sec-fetch-site"],
            api["sec-fetch-mode"],
            api["sec-fetch-dest"],
        ) == (
            "*/*",
            "same-origin",
            "cors",
            "empty",
        )
        # curl sends `priority` only over HTTP/2, as Chrome does; this local server speaks HTTP/1.1.
        assert "upgrade-insecure-requests" not in api and "sec-fetch-user" not in api
    assert search["referer"] == "https://x.com/search?q=%24NVDA%20lang%3Aen&src=typed_query&f=live"
    assert "origin" not in search
    assert post["origin"] == "https://x.com" and post["referer"] == "https://x.com/"
    assert (page["sec-fetch-mode"], page["sec-fetch-dest"]) == ("navigate", "document")


# Fake proxy credentials; a leak shows up as these strings in a log line or an exception text.
FAKE_PROXY_USER = "fakeproxyuser"
FAKE_PROXY_PASS = "fakeproxypass"


@pytest.mark.parametrize(
    ("impersonate", "proxy"),
    [
        # libcurl echoes the whole proxy URL in "Unsupported proxy syntax in '...'".
        ("chrome", f"http://{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:notaport"),
        # httpx echoes a scheme-less proxy URL in "Unknown scheme for proxy URL ...".
        (None, f"{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:notaport"),
        (None, f"http://{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:notaport"),
    ],
)
async def test_proxy_credentials_never_reach_logs_or_errors(
    tmp_path, monkeypatch, impersonate, proxy
):
    """A credentialed, malformed account proxy on the real request path (cached signer, retries, cooldown):
    neither a log line at any level nor the raised error carries the proxy username or password."""
    from types import SimpleNamespace

    from loguru import logger

    import twscrape.queue_client as queue_client_module
    from twscrape.accounts_pool import AccountsPool
    from twscrape.queue_client import QueueClient

    async def no_sleep(*args, **kwargs):
        return None

    # Only the queue client's retry pauses; curl-cffi's own event-loop timers need the real asyncio.sleep.
    monkeypatch.setattr(queue_client_module, "asyncio", SimpleNamespace(sleep=no_sleep))
    monkeypatch.delenv("TWS_PROXY", raising=False)
    monkeypatch.delenv("TWS_IMPERSONATE", raising=False)
    pool = AccountsPool(str(tmp_path / "accounts.db"), raise_when_no_account=True)
    await pool.add_account("u1", "_", "_", "_", proxy=proxy, cookies="auth_token=tok; ct0=csrf")
    client = QueueClient(pool, "SearchTimeline", impersonate=impersonate)

    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="TRACE")
    try:
        with pytest.raises(Exception) as exc_info:
            await client.get("http://127.0.0.1:9/i/api/graphql/x/SearchTimeline")
    finally:
        logger.remove(sink)
        await client._close_ctx()

    error = exc_info.value
    texts = [*lines, str(error), repr(error)]
    cause = error.__cause__ or error.__context__
    while cause is not None:
        texts += [str(cause), repr(cause)]
        cause = cause.__cause__ or cause.__context__
    for text in texts:
        assert FAKE_PROXY_USER not in text and FAKE_PROXY_PASS not in text, text
    if impersonate:
        assert any("Transport error" in line for line in lines)  # the WARNING after the retries


def _exception_texts(error: BaseException) -> list[str]:
    """str and repr of an error and of everything chained to it (__cause__, __context__, group members)."""
    texts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        texts += [str(current), repr(current)]
        pending += [e for e in (current.__cause__, current.__context__) if e is not None]
        pending += list(getattr(current, "exceptions", ()))
    return texts


def _assert_no_proxy_secret(texts: list[str]) -> None:
    for text in texts:
        assert FAKE_PROXY_USER not in text and FAKE_PROXY_PASS not in text, text


@pytest.mark.parametrize(
    ("impersonate", "proxy"),
    [
        ("chrome", f"http://{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:notaport"),
        (None, f"{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:notaport"),
        (None, f"http://{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:notaport"),
        (None, f"http://{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:9"),
    ],
)
async def test_transport_errors_carry_no_credentials_anywhere_in_their_chain(
    monkeypatch, impersonate, proxy
):
    """Sentry, loguru's diagnose and exc_info formatters print __cause__ and __context__: the original libcurl or
    httpx error with the proxy URL must not hang on the clean one."""
    from twscrape.account import make_transport

    monkeypatch.delenv("TWS_PROXY", raising=False)
    with pytest.raises(Exception) as exc_info:
        transport = make_transport(proxy, impersonate)
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("http://127.0.0.1:9/i/api/graphql/x/SearchTimeline")

    _assert_no_proxy_secret(_exception_texts(exc_info.value))


async def test_saving_an_account_logs_no_proxy_credentials(tmp_path, caplog):
    """aiosqlite logs every call with its SQL parameters at DEBUG, the account's proxy and cookies among them."""
    import logging

    from loguru import logger

    from twscrape.accounts_pool import AccountsPool

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="aiosqlite")
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="TRACE")
    try:
        pool = AccountsPool(str(tmp_path / "accounts.db"))
        proxy = f"http://{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:8080"
        await pool.add_account("u1", "_", "_", "_", proxy=proxy, cookies="auth_token=t; ct0=c")
        account = await pool.get("u1")
        await pool.save(account)
    finally:
        logger.remove(sink)

    assert (await pool.get("u1")).proxy == proxy
    _assert_no_proxy_secret([*lines, caplog.text, *(r.getMessage() for r in caplog.records)])


# Proxy URLs that do not parse cleanly: a raw "/" in the password, and ":" and "@" in it. Their fragments of 3 or
# more characters must not appear anywhere.
UNPARSEABLE_PROXIES = [
    (
        "http://fakeproxyuser:fakepa%40ss/w0rdsecret@127.0.0.1:9",
        ["fakeproxyuser", "fakepa", "w0rdsecret"],
    ),
    (
        "http://fakeproxyuser:pw1x:secr3t@w0rdz@127.0.0.1:9",
        ["fakeproxyuser", "pw1x", "secr3t", "w0rdz"],
    ),
    # A full-width colon becomes ":" under NFKC: urlsplit() refuses the netloc with a ValueError that quotes it,
    # credentials included (review round 3 of v0.22.2).
    (
        "http://fakeproxyuser:fakepa\uff1assword@127.0.0.1:9",
        ["fakeproxyuser", "fakepa", "ssword"],
    ),
]


@pytest.mark.parametrize("impersonate", [None, "chrome"])
@pytest.mark.parametrize(("proxy", "fragments"), UNPARSEABLE_PROXIES)
async def test_no_fragment_of_an_unparseable_proxy_password_leaks(
    tmp_path, monkeypatch, impersonate, proxy, fragments
):
    """httpx reports "Invalid port: 'fakepa%40ss'" and curl "'http://***@w0rdz@127.0.0.1:9'": pieces of the
    password that no exact-substring rule catches. The whole message is replaced, the type is kept."""
    from types import SimpleNamespace

    from loguru import logger

    import twscrape.queue_client as queue_client_module
    from twscrape.account import make_transport
    from twscrape.accounts_pool import AccountsPool
    from twscrape.queue_client import QueueClient

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(queue_client_module, "asyncio", SimpleNamespace(sleep=no_sleep))
    monkeypatch.delenv("TWS_PROXY", raising=False)
    monkeypatch.delenv("TWS_IMPERSONATE", raising=False)

    with pytest.raises(Exception) as direct:
        transport = make_transport(proxy, impersonate)
        async with httpx.AsyncClient(transport=transport) as http:
            await http.get("http://127.0.0.1:9/i/api/graphql/x/SearchTimeline")
    assert isinstance(direct.value, (httpx.TransportError, httpx.InvalidURL))
    assert "credentials removed" in str(direct.value)

    pool = AccountsPool(str(tmp_path / "accounts.db"), raise_when_no_account=True)
    await pool.add_account("u1", "_", "_", "_", proxy=proxy, cookies="auth_token=tok; ct0=csrf")
    client = QueueClient(pool, "SearchTimeline", impersonate=impersonate)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="TRACE")
    try:
        with pytest.raises(Exception) as via_client:
            await client.get("http://127.0.0.1:9/i/api/graphql/x/SearchTimeline")
    finally:
        logger.remove(sink)
        await client._close_ctx()

    texts = [*lines, *_exception_texts(direct.value), *_exception_texts(via_client.value)]
    for text in texts:
        for fragment in fragments:
            assert fragment not in text, text


@pytest.mark.parametrize("impersonate", [None, "chrome"])
@pytest.mark.parametrize(
    "proxy",
    [
        f"http://{FAKE_PROXY_USER}:{FAKE_PROXY_PASS}@127.0.0.1:notaport",
        "http://fakeproxyuser:fakepa%40ss/w0rdsecret@127.0.0.1:9",
    ],
)
async def test_a_clean_transport_error_is_raised_without_context(monkeypatch, impersonate, proxy):
    """CI runs 44921978929 and 44921942025 failed on Python 3.10 only. There the CurlError that curl-cffi handled
    inside its awaited coroutine (session.py, `except CurlError`) is still in sys.exc_info() after our except
    block, so a plain `raise` after the block chained the libcurl error, credentials included, as __context__.
    On 3.11+ the same code raised without context, so this test is red on 3.10 only."""
    from twscrape.account import make_transport

    monkeypatch.delenv("TWS_PROXY", raising=False)
    with pytest.raises(Exception) as exc_info:
        transport = make_transport(proxy, impersonate)
        await transport.handle_async_request(
            httpx.Request("GET", "http://127.0.0.1:9/i/api/graphql/x/SearchTimeline")
        )

    assert exc_info.value.__context__ is None and exc_info.value.__cause__ is None
    assert "credentials removed" in str(exc_info.value)
