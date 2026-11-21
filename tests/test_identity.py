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
