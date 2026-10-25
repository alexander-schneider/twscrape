import asyncio
import copy
import json
import logging
from http.cookies import SimpleCookie

import httpx
import pytest

import twscrape.models as models
from twscrape.queue_client import XClIdGenStore

DATE = "Sat Oct 24 12:00:00 +0000 2026"
OLD_DATE = "Fri Oct 23 23:59:59 +0000 2026"


def author(uid="42", handle="full_author", **fields):
    return {
        "__typename": "User",
        "rest_id": uid,
        "core": {"screen_name": handle, "name": "Full Author", "created_at": DATE},
        "legacy": {
            "description": "Literal preserved bio",
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
            "full_text": text or f"Literal tweet {tid}",
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


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        if record.name in {"aiosqlite", "httpx", "httpcore.connection", "httpcore.http11"}:
            return
        self.records.append(
            {"name": record.name, "level": record.levelname, "message": record.getMessage()}
        )


class PipelineReplay:
    def __init__(self, monkeypatch, tmp_path, consumer=None):
        self.consumer_modules = consumer
        self.responses = []
        self.requests = []
        self.signatures = []
        self.sleeps = []
        self.logs = Capture()
        self.log_root = logging.getLogger()
        self.old_level = self.log_root.level
        self.log_root.setLevel(logging.INFO)
        self.log_root.addHandler(self.logs)
        real_sleep = asyncio.sleep
        self.real_sleep = real_sleep

        async def shortened_sleep(delay, *args, **kwargs):
            self.sleeps.append(delay)
            return await real_sleep(0)

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
            assert proxy and proxy.startswith("http://")
            assert account.username.startswith("offline-")
            return Signer()

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", http_transport)
        monkeypatch.setattr(XClIdGenStore, "get", classmethod(signer))
        monkeypatch.setattr(asyncio, "sleep", shortened_sleep)
        monkeypatch.setattr(models, "PARSE_ERROR_DUMP_DIR", str(tmp_path / "parser-dumps"))
        models.PARSE_ERROR_DUMP_WRITER.reset()
        if consumer is not None:
            monkeypatch.setattr(consumer.settings, "tws_proxy", "http://offline.invalid:8080")
            monkeypatch.setattr(consumer.settings, "x_scrape_max_search_attempts", 2)
            monkeypatch.setattr(consumer.settings, "x_scrape_tweets_per_symbol", 2)
            monkeypatch.setattr(consumer.settings, "x_scrape_replies_per_tweet", 50)

    def enqueue(self, *bodies, operation="SearchTimeline", status=200):
        self.responses.extend((operation, body, status) for body in bodies)

    async def consumer(self, tmp_path, accounts=1):
        assert self.consumer_modules is not None
        wrapper = self.consumer_modules.XAccountPool(str(tmp_path / "invented-accounts.db"))
        api = await wrapper.get_api()
        for i in range(accounts):
            await api.pool.add_account(
                f"offline-{i}",
                "_",
                "_",
                "_",
                user_agent="offline-verification/1.0",
                cookies=f"auth_token=dummy-offline-{i}; ct0=dummy-csrf-offline-{i}",
            )
        assert await wrapper.get_active_count() == accounts
        return wrapper, api, self.consumer_modules.XTweetScraper(wrapper)

    def close(self):
        self.log_root.removeHandler(self.logs)
        self.log_root.setLevel(self.old_level)
