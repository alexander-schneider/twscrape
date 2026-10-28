import asyncio
import copy
import json
from http.cookies import SimpleCookie

import httpx
import pytest

from twscrape.account import Account
from twscrape.queue_client import XClIdGenStore

DATE = "Sat Oct 24 12:00:00 +0000 2026"


def author(uid="42", handle="example_reader", **fields):
    return {
        "__typename": "User",
        "rest_id": uid,
        "core": {"screen_name": handle, "name": "Example Reader", "created_at": DATE},
        "legacy": {
            "description": "An invented profile",
            "followers_count": 321,
            "friends_count": 12,
            "profile_image_url_https": "https://images.invalid/42.jpg",
            **fields,
        },
    }


def tweet(
    tid,
    *,
    text=None,
    uid="42",
    profile: str | dict | None = "full",
    date=DATE,
    parent=None,
    **fields,
):
    obj = {
        "__typename": "Tweet",
        "rest_id": str(tid),
        "legacy": {
            "id_str": str(tid),
            "user_id_str": str(uid),
            "created_at": date,
            "full_text": text if text is not None else f"Example post {tid}",
            "lang": "en",
            "favorite_count": 7,
            "retweet_count": 3,
            "reply_count": 2,
            "quote_count": 1,
            "entities": {},
            **fields,
        },
        "views": {"count": "1234"},
    }
    if parent is not None:
        obj["legacy"]["in_reply_to_status_id_str"] = str(parent)
    if profile == "full":
        obj["core"] = {"user_results": {"result": author(uid)}}
    elif isinstance(profile, dict):
        obj["core"] = {"user_results": {"result": profile}}
    return obj


def page(*tweets, cursor=None, cursor_type="Bottom", shadows=()):
    entries = [
        {
            "entryId": f"tweet-{tw['rest_id']}",
            "content": {"itemContent": {"tweet_results": {"result": tw}}},
        }
        for tw in tweets
    ]
    if cursor is not None:
        entries.append(
            {
                "entryId": f"cursor-{cursor}",
                "content": {"cursorType": cursor_type, "value": cursor},
            }
        )
    return {
        "data": {
            "timeline": {
                "instructions": [{"type": "TimelineAddEntries", "entries": entries}],
                "shadows": list(shadows),
            }
        }
    }


def error_body(message, code=-1):
    return {"data": {"user": {}}, "errors": [{"code": code, "message": message}]}


class PipelineReplay:
    def __init__(self, monkeypatch, tmp_path):
        self.responses = []
        self.requests = []
        self.signatures = []
        self.sleeps = []
        self.clients = []
        self.real_sleep = asyncio.sleep
        make_client = Account.make_client

        def tracked_client(account, proxy=None):
            client = make_client(account, proxy=proxy)
            self.clients.append(client)
            return client

        async def shortened_sleep(delay, *args, **kwargs):
            self.sleeps.append(delay)
            return await self.real_sleep(0)

        async def http_transport(_transport, request):
            if request.method == "POST":
                payload = json.loads(request.content)
            else:
                payload = {
                    k: json.loads(v)
                    for k, v in request.url.params.items()
                    if k in ("variables", "features", "fieldToggles")
                }
            cookies = SimpleCookie()
            cookies.load(request.headers.get("cookie", ""))
            account = (
                cookies["auth_token"].value.removeprefix("dummy-")
                if "auth_token" in cookies
                else None
            )
            self.requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "operation": request.url.path.rsplit("/", 1)[-1],
                    "payload": payload,
                    "dummy_account": account,
                    "signed": request.headers.get("x-client-transaction-id")
                    == "offline-signature",
                    "csrf_set": request.headers.get("x-csrf-token") == f"dummy-csrf-{account}",
                    "authorized": request.headers.get("authorization", "").startswith("Bearer "),
                }
            )
            if not self.responses:
                pytest.fail(f"Unscripted external request: {request.url.path}")
            operation, body, status = self.responses.pop(0)
            if operation != self.requests[-1]["operation"]:
                pytest.fail(
                    f"Expected operation {operation}, got {self.requests[-1]['operation']}"
                )
            return httpx.Response(status, json=copy.deepcopy(body), request=request)

        class Signer:
            def calc(_self, method, path):
                self.signatures.append({"method": method, "path": path})
                return "offline-signature"

        async def signer(_cls, account, proxy=None, fresh=False):
            return Signer()

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", http_transport)
        monkeypatch.setattr(Account, "make_client", tracked_client)
        monkeypatch.setattr(XClIdGenStore, "get", classmethod(signer))
        monkeypatch.setattr(asyncio, "sleep", shortened_sleep)

    def enqueue(self, *bodies, operation="SearchTimeline", status=200):
        self.responses.extend((operation, body, status) for body in bodies)

    def close(self):
        assert not self.responses, "Scripted HTTP responses were not exercised"
        assert all(client.is_closed for client in self.clients), "HTTP client was not closed"
