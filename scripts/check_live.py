"""Run a bounded live check of the public API and write a credential-free report."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, aclosing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

import twscrape  # noqa: E402
from twscrape import API, AccountsPool, models  # noqa: E402
from twscrape.logger import logger  # noqa: E402
from twscrape.queue_client import XClIdGenStore  # noqa: E402
from twscrape.utils import parse_cookies  # noqa: E402

HTTP_BUDGET = 24
OPERATION_TIMEOUT = 45
OVERALL_TIMEOUT = 180
DEFAULT_QUERY = "hello lang:en min_replies:1"


class ProbeFailure(Exception):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def candidate_identity():
    imported = Path(twscrape.__file__).resolve()
    if imported != ROOT / "twscrape" / "__init__.py":
        raise ProbeFailure("candidate_import")
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()
    return {"candidate_sha": sha, "candidate_import": imported.relative_to(ROOT).as_posix()}


def live_config(environ):
    proxy = environ.get("TWS_LIVE_PROXY", "")
    parsed = urlparse(proxy)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ProbeFailure("configuration")
    username = environ.get("TWS_LIVE_USERNAME", "")
    cookies = environ.get("TWS_LIVE_COOKIES", "")
    db = environ.get("TWS_LIVE_DB", "")
    if username or cookies:
        if not username or not cookies:
            raise ProbeFailure("configuration")
        try:
            parsed_cookies = parse_cookies(cookies)
        except ValueError:
            raise ProbeFailure("configuration") from None
        if not all(parsed_cookies.get(k) for k in ("auth_token", "ct0")):
            raise ProbeFailure("configuration")
    elif not db or not Path(db).is_file():
        raise ProbeFailure("configuration")
    return SimpleNamespace(proxy=proxy, username=username, cookies=cookies, db=db)


def backup_database(source, destination, *, deadline=None):
    operation_deadline = time.monotonic() + OPERATION_TIMEOUT
    deadline = operation_deadline if deadline is None else min(operation_deadline, deadline)

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise ProbeFailure("timeout")

    # SQLite backup includes committed WAL state without changing the source database.
    with sqlite3.connect(
        Path(source).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.01
    ) as original:
        with sqlite3.connect(destination) as copied:
            original.backup(copied, pages=100, progress=progress, sleep=0.01)


def validate_tweet(tweet, *, parent=None):
    if not isinstance(tweet, models.Tweet) or not isinstance(tweet.user, models.User):
        raise ProbeFailure("model_contract")
    if (
        type(tweet.id) is not int
        or tweet.id <= 0
        or tweet.id_str != str(tweet.id)
        or type(tweet.user.id) is not int
        or tweet.user.id <= 0
        or tweet.user.id_str != str(tweet.user.id)
        or not isinstance(tweet.user.username, str)
        or not isinstance(tweet.rawContent, str)
        or not isinstance(tweet.date, datetime)
        or tweet.date.utcoffset() != timedelta(0)
        or not isinstance(tweet.user.created, datetime)
        or (tweet.user.created != datetime.min and tweet.user.created.utcoffset() != timedelta(0))
    ):
        raise ProbeFailure("model_contract")
    counters = (
        tweet.likeCount,
        tweet.retweetCount,
        tweet.replyCount,
        tweet.quoteCount,
        tweet.bookmarkedCount,
        tweet.user.followersCount,
        tweet.user.friendsCount,
        tweet.user.statusesCount,
        tweet.user.favouritesCount,
        tweet.user.listedCount,
        tweet.user.mediaCount,
    )
    if any(type(value) is not int or value < 0 for value in counters):
        raise ProbeFailure("model_contract")
    if tweet.viewCount is not None and (type(tweet.viewCount) is not int or tweet.viewCount < 0):
        raise ProbeFailure("model_contract")
    if parent is not None and tweet.inReplyToTweetId != parent:
        raise ProbeFailure("direct_reply_contract")


def validate_tweets(tweets, *, parent=None):
    for tweet in tweets:
        validate_tweet(tweet, parent=parent)
    if len({tweet.id for tweet in tweets}) != len(tweets):
        raise ProbeFailure("duplicate_ids")


class HTTPAudit:
    def __init__(self):
        self.requests = 0
        self.failure = None

    async def send(self, original, client, request, *args, **kwargs):
        if self.requests >= HTTP_BUDGET:
            self.failure = "request_budget"
            raise ProbeFailure(self.failure)
        self.requests += 1
        graphql = "/graphql/" in request.url.path
        if graphql and request.headers.get("x-client-transaction-id", "") in {
            "",
            "mocked-clid",
            "offline-signature",
        }:
            self.failure = "signing"
            raise ProbeFailure(self.failure)
        try:
            response = await original(client, request, *args, **kwargs)
            await response.aread()
        except Exception:
            self.failure = self.failure or "transport"
            raise
        if response.status_code >= 400:
            self.failure = "http_response"
        if graphql:
            try:
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("errors"):
                    self.failure = "graphql_response"
            except (ValueError, httpx.ResponseNotRead):
                self.failure = "graphql_response"
        return response

    def require_clean(self):
        if self.failure:
            raise ProbeFailure(self.failure)


async def collect(generator):
    async with aclosing(generator):
        return [item async for item in generator]


async def probe(config, query, report, db_file):
    deadline = time.monotonic() + OVERALL_TIMEOUT
    if not config.username:
        backup_database(config.db, db_file, deadline=deadline)
    pool = AccountsPool(str(db_file), raise_when_no_account=True)
    if config.username:
        await asyncio.wait_for(
            pool.add_account(
                config.username,
                "_",
                "_",
                "_",
                cookies=config.cookies,
                proxy=config.proxy,
                user_agent="Mozilla/5.0",
            ),
            OPERATION_TIMEOUT,
        )
    accounts = await asyncio.wait_for(pool.accounts_info(), OPERATION_TIMEOUT)
    if not any(account["active"] and account["logged_in"] for account in accounts):
        raise ProbeFailure("configuration")
    api = API(pool, proxy=config.proxy, raise_when_no_account=True)
    audit = HTTPAudit()
    original = httpx.AsyncHTTPTransport.handle_async_request

    async def send(client, request, *args, **kwargs):
        return await audit.send(original, client, request, *args, **kwargs)

    def parser_failure(*args, **kwargs):
        audit.failure = "parse_drift"

    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", send))
        stack.enter_context(patch.object(XClIdGenStore, "items", {}))
        stack.enter_context(patch.object(models.PARSE_ERROR_DUMP_WRITER, "write", parser_failure))
        try:
            tweets = await asyncio.wait_for(
                collect(api.search(query, limit=20)), OPERATION_TIMEOUT
            )
            audit.require_clean()
            if not tweets:
                raise ProbeFailure("empty_search")
            validate_tweets(tweets)
            report["counts"]["search_tweets"] = len(tweets)
            parents = [tweet for tweet in tweets if tweet.replyCount > 0][:3]
            if not parents:
                raise ProbeFailure("no_reply_parent")
            for parent in parents:
                report["counts"]["reply_parents"] += 1
                replies = await asyncio.wait_for(
                    collect(api.tweet_replies(parent.id, limit=20)), OPERATION_TIMEOUT
                )
                audit.require_clean()
                validate_tweets(replies, parent=parent.id)
                report["counts"]["direct_replies"] += len(replies)
                if replies:
                    break
            if not report["counts"]["direct_replies"]:
                raise ProbeFailure("empty_replies")
        except Exception:
            audit.require_clean()
            raise
        finally:
            report["counts"]["http_requests"] = audit.requests


async def run(environ=None, query=DEFAULT_QUERY):
    environ = os.environ if environ is None else environ
    report = {
        "mode": "live",
        "status": "fail",
        "category": "configuration",
        "error_class": None,
        "counts": {
            "http_requests": 0,
            "search_tweets": 0,
            "reply_parents": 0,
            "direct_replies": 0,
        },
    }
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="tws-live-") as temporary:
            logger.disable("")
            report.update(candidate_identity())
            config = live_config(environ)
            if not isinstance(query, str) or not query.strip():
                raise ProbeFailure("configuration")
            await asyncio.wait_for(
                probe(config, query, report, Path(temporary) / "accounts.db"), OVERALL_TIMEOUT
            )
            await asyncio.sleep(0.05)
            if unhandled:
                raise ProbeFailure("async_cleanup")
            report.update(status="pass", category="accepted")
    except ProbeFailure as error:
        report.update(category=error.category, error_class=type(error).__name__)
    except asyncio.TimeoutError as error:
        report.update(category="timeout", error_class=type(error).__name__)
    except Exception as error:
        report.update(category="operation_error", error_class=type(error).__name__)
    finally:
        logger.enable("")
        logging.disable(previous_disable)
        loop.set_exception_handler(previous_handler)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    args = parser.parse_args()
    report = asyncio.run(run(query=args.query))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
