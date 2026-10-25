"""Bounded read-only X acceptance with an allowlisted, credential-free report."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, aclosing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

import twscrape  # noqa: E402
from twscrape import (  # noqa: E402
    API,
    AccountsPool,
    models,  # noqa: E402
)
from twscrape.logger import logger  # noqa: E402
from twscrape.queue_client import XClIdGenStore  # noqa: E402
from twscrape.utils import parse_cookies  # noqa: E402

HTTP_BUDGET = 24
OPERATION_TIMEOUT = 45
OVERALL_TIMEOUT = 180


class ProbeFailure(Exception):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def git_sha(root):
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
    ).strip()


def candidate_identity():
    imported = Path(twscrape.__file__).resolve()
    if imported != ROOT / "twscrape" / "__init__.py":
        raise ProbeFailure("candidate_import")
    return {"candidate_sha": git_sha(ROOT), "candidate_import": str(imported)}


def load_consumer(source):
    root = Path(source).resolve()
    if not root.is_dir():
        raise ProbeFailure("consumer_source")
    sys.path.insert(0, str(root))
    names = [
        "shared.config",
        "stocks.app.platforms.x.scraper.account_pool",
        "stocks.app.platforms.x.scraper.client",
        "stocks.app.platforms.x.scraper.twscrape_logging",
    ]
    modules = [importlib.import_module(name) for name in names]
    paths = {}
    for name, module in zip(names, modules):
        if module.__file__ is None:
            raise ProbeFailure("consumer_import")
        paths[name] = str(Path(module.__file__).resolve())
    for name, module in list(sys.modules.items()):
        if name.startswith("shared.") and module is not None and module.__file__:
            paths[name] = str(Path(module.__file__).resolve())
    if any(not Path(path).is_relative_to(root) for path in paths.values()):
        raise ProbeFailure("consumer_import")
    candidate_identity()
    return SimpleNamespace(
        settings=modules[0].settings,
        XAccountPool=modules[1].XAccountPool,
        XAccountPoolError=modules[1].XAccountPoolError,
        XTweetScraper=modules[2].XTweetScraper,
        ReplyFetchBudget=modules[2].ReplyFetchBudget,
        identity={"consumer_sha": git_sha(root), "consumer_imports": paths},
    )


def live_config(environ):
    proxy = environ.get("TWS_LIVE_PROXY", "")
    parsed = urlparse(proxy)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ProbeFailure("configuration")
    username = environ.get("TWS_LIVE_USERNAME", "")
    cookies = environ.get("TWS_LIVE_COOKIES", "")
    db = environ.get("TWS_LIVE_DB", "")
    if username or cookies:
        if not username or not all(parse_cookies(cookies).get(k) for k in ("auth_token", "ct0")):
            raise ProbeFailure("configuration")
    elif not db or not Path(db).is_file():
        raise ProbeFailure("configuration")
    source = environ.get("ADANOS_SOURCE_ROOT", "")
    if environ.get("TWS_REQUIRE_ADANOS") == "1" and not source:
        raise ProbeFailure("consumer_source")
    return SimpleNamespace(proxy=proxy, username=username, cookies=cookies, db=db, source=source)


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


def validate_row(row, *, parent=None):
    required = {
        "tweet_id",
        "author_id",
        "author_username",
        "text",
        "likes",
        "retweets",
        "replies_count",
        "views",
        "created_at",
        "is_reply",
        "parent_tweet_id",
    }
    if not required <= row.keys():
        raise ProbeFailure("model_contract")
    if not row["tweet_id"] or not row["author_id"] or not isinstance(row["text"], str):
        raise ProbeFailure("model_contract")
    if not isinstance(row["author_username"], str) or not isinstance(row["created_at"], datetime):
        raise ProbeFailure("model_contract")
    if row["created_at"].tzinfo is None:
        raise ProbeFailure("model_contract")
    if any(
        not isinstance(row[k], int) or row[k] < 0 for k in ("likes", "retweets", "replies_count")
    ):
        raise ProbeFailure("model_contract")
    if row["views"] is not None and (not isinstance(row["views"], int) or row["views"] < 0):
        raise ProbeFailure("model_contract")
    if parent is not None and (not row["is_reply"] or row["parent_tweet_id"] != parent):
        raise ProbeFailure("direct_reply_contract")


def library_row(tweet, parent=None):
    return {
        "tweet_id": str(tweet.id),
        "author_id": str(tweet.user.id),
        "author_username": tweet.user.username,
        "text": tweet.rawContent,
        "likes": tweet.likeCount,
        "retweets": tweet.retweetCount,
        "replies_count": tweet.replyCount,
        "views": tweet.viewCount,
        "created_at": tweet.date,
        "is_reply": parent is not None,
        "parent_tweet_id": parent,
    }


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


async def probe(config, ticker, report, db_file):
    deadline = time.monotonic() + OVERALL_TIMEOUT
    consumer = load_consumer(config.source) if config.source else None
    if consumer:
        report.update(consumer.identity)
    if not config.username:
        backup_database(config.db, db_file, deadline=deadline)
    pool = AccountsPool(str(db_file), raise_when_no_account=True)
    if config.username:
        await pool.add_account(
            config.username,
            "_",
            "_",
            "_",
            cookies=config.cookies,
            proxy=config.proxy,
            user_agent="Mozilla/5.0",
        )
    accounts = await pool.get_all()
    if not any(
        a.active and all(a.cookies.get(k) for k in ("auth_token", "ct0")) for a in accounts
    ):
        raise ProbeFailure("configuration")
    api = API(pool, proxy=config.proxy, raise_when_no_account=True)
    scraper = None
    with ExitStack() as stack:
        if consumer:
            stack.enter_context(patch.object(consumer.settings, "tws_proxy", config.proxy))
            wrapper = consumer.XAccountPool(str(db_file))
            api = await wrapper.get_api()
            api.pool._raise_when_no_account = True
            scraper = consumer.XTweetScraper(wrapper)
            if await wrapper.get_active_count() < 1:
                raise ProbeFailure("configuration")
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        until = int((start + timedelta(days=1)).timestamp())
        query = f"${ticker} lang:en until_time:{until} since_time:{int(start.timestamp())} -filter:links"
        audit = HTTPAudit()
        original = httpx.AsyncHTTPTransport.handle_async_request

        async def send(client, request, *args, **kwargs):
            return await audit.send(original, client, request, *args, **kwargs)

        def parser_failure(*args, **kwargs):
            audit.failure = "parse_drift"

        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", send))
        stack.enter_context(patch.object(XClIdGenStore, "items", {}))
        stack.enter_context(patch.object(models.PARSE_ERROR_DUMP_WRITER, "write", parser_failure))
        try:
            if scraper:
                outcome = await asyncio.wait_for(
                    scraper.search_cashtag(ticker, now, limit=20), OPERATION_TIMEOUT
                )
                if outcome.degraded:
                    raise ProbeFailure("partial_search")
                rows = outcome.tweets
            else:
                tweets = await asyncio.wait_for(
                    collect(api.search(query, limit=20)), OPERATION_TIMEOUT
                )
                rows = [library_row(tweet) for tweet in tweets]
            audit.require_clean()
            if not rows:
                raise ProbeFailure("empty_search")
            for row in rows:
                validate_row(row)
            if not scraper:
                rows = [row for row in rows if row["created_at"] >= start]
            report["counts"]["search_tweets"] = len(rows)
            if not rows or any(
                not start <= row["created_at"] < start + timedelta(days=1) for row in rows
            ):
                raise ProbeFailure("search_day_contract")
            parents = [row for row in rows if row["replies_count"] > 0][:3]
            if not parents:
                raise ProbeFailure("no_reply_parent")
            for parent in parents:
                report["counts"]["reply_parents"] += 1
                parent_id = parent["tweet_id"]
                if scraper:
                    replies = await asyncio.wait_for(
                        scraper.fetch_replies(parent_id, limit=20), OPERATION_TIMEOUT
                    )
                else:
                    tweets = await asyncio.wait_for(
                        collect(api.tweet_replies(int(parent_id), limit=20)), OPERATION_TIMEOUT
                    )
                    replies = [library_row(tweet, parent_id) for tweet in tweets]
                audit.require_clean()
                for row in replies:
                    validate_row(row, parent=parent_id)
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


async def run(environ=None, ticker="NVDA"):
    environ = os.environ if environ is None else environ
    report = {
        "mode": "consumer-live" if environ.get("ADANOS_SOURCE_ROOT") else "library-live",
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
            if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", ticker):
                raise ProbeFailure("configuration")
            await asyncio.wait_for(
                probe(config, ticker, report, Path(temporary) / "accounts.db"), OVERALL_TIMEOUT
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
    parser.add_argument("--ticker", default="NVDA")
    args = parser.parse_args()
    report = asyncio.run(run(ticker=args.ticker))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
