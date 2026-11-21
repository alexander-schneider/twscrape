import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

from httpx import AsyncBaseTransport, AsyncClient, AsyncHTTPTransport

from .models import JSONTrait
from .utils import utc


def resolve_proxy(account_proxy: str | None, api_proxy: str | None) -> str | None:
    """The account's own proxy first, so each account keeps one exit; then the API proxy, then TWS_PROXY."""
    for proxy in (account_proxy, api_proxy, os.getenv("TWS_PROXY")):
        if proxy:
            return proxy
    return None


def resolve_impersonate(impersonate: str | None) -> str | None:
    """A curl-impersonate browser profile (e.g. "chrome") from the argument or TWS_IMPERSONATE; None keeps httpx."""
    return impersonate or os.getenv("TWS_IMPERSONATE") or None


def make_transport(proxy: str | None, impersonate: str | None) -> AsyncBaseTransport:
    if impersonate:
        from .curl_transport import CurlTransport

        return CurlTransport(impersonate, proxy=proxy)
    return AsyncHTTPTransport(retries=3, proxy=proxy)


TOKEN = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"


@dataclass
class Account(JSONTrait):
    username: str
    password: str
    email: str
    email_password: str
    user_agent: str
    active: bool
    locks: dict[str, datetime] = field(default_factory=dict)  # queue: datetime
    stats: dict[str, int] = field(default_factory=dict)  # queue: requests
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    mfa_code: str | None = None
    proxy: str | None = None
    error_msg: str | None = None
    last_used: datetime | None = None
    _tx: str | None = None

    @property
    def has_session(self) -> bool:
        return all(self.cookies.get(name) for name in ("auth_token", "ct0"))

    @property
    def login_method(self) -> Literal["cookies", "password"]:
        return "cookies" if self.password == "_" else "password"

    @staticmethod
    def from_rs(rs: sqlite3.Row):
        doc = dict(rs)
        doc["locks"] = {k: utc.from_iso(v) for k, v in json.loads(doc["locks"]).items()}
        doc["stats"] = {k: v for k, v in json.loads(doc["stats"]).items() if isinstance(v, int)}
        doc["headers"] = json.loads(doc["headers"])
        doc["cookies"] = json.loads(doc["cookies"])
        doc["active"] = bool(doc["active"])
        doc["last_used"] = utc.from_iso(doc["last_used"]) if doc["last_used"] else None
        return Account(**doc)

    def to_rs(self):
        rs = asdict(self)
        rs["locks"] = json.dumps(rs["locks"], default=lambda x: x.isoformat())
        rs["stats"] = json.dumps(rs["stats"])
        rs["headers"] = json.dumps(rs["headers"])
        rs["cookies"] = json.dumps(rs["cookies"])
        rs["last_used"] = rs["last_used"].isoformat() if rs["last_used"] else None
        return rs

    def make_client(self, proxy: str | None = None, impersonate: str | None = None) -> AsyncClient:
        proxy = resolve_proxy(self.proxy, proxy)
        impersonate = resolve_impersonate(impersonate)
        client = AsyncClient(follow_redirects=True, transport=make_transport(proxy, impersonate))

        # saved from previous usage
        client.cookies.update(self.cookies)
        client.headers.update(self.headers)

        # default settings
        client.headers["user-agent"] = self.user_agent
        client.headers["content-type"] = "application/json"
        client.headers["authorization"] = TOKEN
        client.headers["x-twitter-active-user"] = "yes"
        client.headers["x-twitter-client-language"] = "en"

        if "ct0" in client.cookies:
            client.headers["x-csrf-token"] = client.cookies["ct0"]

        return client
