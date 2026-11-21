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

    import twscrape.queue_client as queue_client_module
    from twscrape.accounts_pool import AccountsPool
    from twscrape.logger import logger
    from twscrape.queue_client import QueueClient

    async def no_sleep(*args, **kwargs):
        return None

    # Only the queue client's retry pauses; curl-cffi's own event-loop timers need the real asyncio.sleep.
    monkeypatch.setattr(queue_client_module, "asyncio", SimpleNamespace(sleep=no_sleep))
    monkeypatch.delenv("TWS_PROXY", raising=False)
    monkeypatch.delenv("TWS_IMPERSONATE", raising=False)
    pool = AccountsPool(str(tmp_path / "accounts.db"), raise_when_no_account=True)
    await pool.add_account(
        "u1", "_", "_", "_", proxy=proxy, cookies="auth_token=tok; ct0=csrf"
    )
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
