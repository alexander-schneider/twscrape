import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import check_adanos_live as live
from tests.http_replay import PipelineReplay, error_body, page, tweet
from twscrape.models import parse_tweets
from twscrape.queue_client import XClIdGenStore
from twscrape.xclid import XClIdGen

REAL_SIGNER_GET = XClIdGenStore.get.__func__
ENV = {
    "TWS_LIVE_PROXY": "http://invented-proxy.invalid:8080",
    "TWS_LIVE_USERNAME": "offline-0",
    "TWS_LIVE_COOKIES": "auth_token=dummy-offline-0; ct0=dummy-csrf-offline-0",
}


@pytest.fixture
async def probe_wire(monkeypatch, tmp_path):
    replay = PipelineReplay(monkeypatch, tmp_path)
    monkeypatch.setattr(XClIdGenStore, "get", classmethod(REAL_SIGNER_GET))
    monkeypatch.setattr(XClIdGenStore, "items", {})

    async def create(**kwargs):
        return XClIdGen([1] * 32, "synthetic-animation")

    monkeypatch.setattr(XClIdGen, "create", staticmethod(create))
    yield replay
    await replay.real_sleep(0.05)
    replay.close()
    assert not replay.responses


def current_tweet(tid, **kwargs):
    return tweet(
        tid, date=datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y"), **kwargs
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"TWS_LIVE_PROXY": ""},
        {"TWS_LIVE_USERNAME": ""},
        {"TWS_LIVE_COOKIES": "auth_token=; ct0=nonempty"},
        {"TWS_LIVE_COOKIES": "auth_token=nonempty; ct0="},
        {"TWS_REQUIRE_ADANOS": "1"},
    ],
)
async def test_missing_live_inputs_are_failures_without_http(monkeypatch, updates):
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid configuration must not make HTTP requests")

    monkeypatch.setattr(live.httpx.AsyncClient, "send", forbidden)
    report = await live.run(ENV | updates)
    assert report["status"] == "fail"
    assert report["category"] in {"configuration", "consumer_source"}
    assert report["counts"]["http_requests"] == 0


async def test_positive_library_probe_uses_real_pipeline_and_literal_day_query(probe_wire):
    probe_wire.enqueue(page(current_tweet(101)))
    probe_wire.enqueue(
        page(current_tweet(102, parent=101), current_tweet(103, parent=999)),
        operation="TweetDetail",
    )
    report = await live.run(ENV)
    assert report["mode"] == "library-live" and report["status"] == "pass"
    assert report["category"] == "accepted"
    assert report["counts"] == {
        "http_requests": 2,
        "search_tweets": 1,
        "reply_parents": 1,
        "direct_replies": 1,
    }
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    assert probe_wire.requests[0]["payload"]["variables"]["rawQuery"] == (
        f"$NVDA lang:en until_time:{int(start.timestamp()) + 86400} since_time:{int(start.timestamp())} -filter:links"
    )
    serialized = json.dumps(report)
    assert all(
        value not in serialized
        for value in (
            "dummy-offline-0",
            "dummy-csrf",
            "invented-proxy",
            "Literal tweet",
            "full_author",
        )
    )
    assert "consumer_sha" not in report


async def test_library_probe_accepts_current_day_search_with_older_nested_quote(probe_wire):
    current = current_tweet(111)
    old_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%a %b %d %H:%M:%S +0000 %Y"
    )
    current["legacy"]["quoted_status_id_str"] = "112"
    current["quoted_status_result"] = {"result": tweet(112, date=old_date)}
    response = page(current)
    assert {item.id for item in parse_tweets(response)} == {111, 112}
    probe_wire.enqueue(response)
    probe_wire.enqueue(page(current_tweet(113, parent=111)), operation="TweetDetail")

    report = await live.run(ENV)

    assert report["status"] == "pass" and report["category"] == "accepted"
    assert report["counts"] == {
        "http_requests": 2,
        "search_tweets": 1,
        "reply_parents": 1,
        "direct_replies": 1,
    }
    assert probe_wire.requests[1]["payload"]["variables"]["focalTweetId"] == "111"


@pytest.mark.parametrize("day_offset", [-1, 1])
async def test_out_of_day_search_cannot_pass(probe_wire, day_offset):
    date = (datetime.now(timezone.utc) + timedelta(days=day_offset)).strftime(
        "%a %b %d %H:%M:%S +0000 %Y"
    )
    probe_wire.enqueue(page(tweet(121, date=date)))

    report = await live.run(ENV)

    assert report["status"] == "fail" and report["category"] == "search_day_contract"
    assert report["counts"]["reply_parents"] == 0
    assert report["counts"]["direct_replies"] == 0


@pytest.mark.parametrize(
    "case,category",
    [
        ("search_empty", "empty_search"),
        ("replies_empty", "empty_replies"),
        ("no_parent", "no_reply_parent"),
        ("reply_error", "graphql_response"),
    ],
)
async def test_empty_and_swallowed_reply_errors_cannot_pass(probe_wire, case, category):
    if case == "search_empty":
        probe_wire.enqueue(page())
    else:
        probe_wire.enqueue(page(current_tweet(201, reply_count=0 if case == "no_parent" else 2)))
        if case == "replies_empty":
            probe_wire.enqueue(page(), operation="TweetDetail")
        elif case == "reply_error":
            probe_wire.enqueue(
                error_body("Synthetic server problem", code=999), operation="TweetDetail"
            )
    report = await live.run(ENV)
    assert report["status"] == "fail" and report["category"] == category
    assert report["counts"]["direct_replies"] == 0


async def test_request_budget_stops_actual_http_before_budget_is_exceeded(probe_wire, monkeypatch):
    monkeypatch.setattr(live, "HTTP_BUDGET", 2)
    probe_wire.enqueue(
        page(current_tweet(301), cursor="one"), page(current_tweet(302), cursor="two")
    )
    report = await live.run(ENV)
    assert report["status"] == "fail" and report["category"] == "request_budget"
    assert len(probe_wire.requests) == 2


async def test_operation_deadline_cancels_real_http_pipeline(probe_wire, monkeypatch):
    monkeypatch.setattr(live, "OPERATION_TIMEOUT", 0.01)
    probe_wire.enqueue(page(current_tweet(401)))
    original = live.httpx.AsyncHTTPTransport.handle_async_request

    async def delayed(transport, request):
        response = await original(transport, request)
        await probe_wire.real_sleep(0.1)
        return response

    monkeypatch.setattr(live.httpx.AsyncHTTPTransport, "handle_async_request", delayed)
    report = await live.run(ENV)
    assert report["status"] == "fail" and report["category"] == "timeout"
    assert report["counts"]["http_requests"] == 1


@pytest.mark.parametrize("case,category", [("parse", "parse_drift"), ("http", "http_response")])
async def test_swallowed_parser_and_http_failures_are_not_empty_success(
    probe_wire, case, category
):
    if case == "parse":
        probe_wire.enqueue(page(tweet(501, date="invalid-date")))
    else:
        probe_wire.enqueue(page(), status=503)
    report = await live.run(ENV)
    assert report["status"] == "fail" and report["category"] == category
    assert report["counts"]["direct_replies"] == 0


def test_sqlite_read_only_backup_includes_wal_and_preserves_source(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "copy.db"
    with sqlite3.connect(source) as original:
        original.execute("PRAGMA journal_mode=WAL")
        original.execute("CREATE TABLE sentinel (value TEXT)")
        original.execute("INSERT INTO sentinel VALUES ('literal committed WAL value')")
        original.commit()
        live.backup_database(source, destination)
        with sqlite3.connect(destination) as copied:
            assert copied.execute("SELECT value FROM sentinel").fetchall() == [
                ("literal committed WAL value",)
            ]
        assert original.execute("SELECT value FROM sentinel").fetchall() == [
            ("literal committed WAL value",)
        ]


@pytest.mark.parametrize("operation_timeout,overall_timeout", [(0.05, 1), (1, 0.05)])
def test_exclusively_locked_sqlite_seed_fails_within_probe_deadline(
    tmp_path, operation_timeout, overall_timeout
):
    code = """
import asyncio
import json
import sqlite3
import sys
import time
from scripts import check_adanos_live as live

live.OPERATION_TIMEOUT = float(sys.argv[2])
live.OVERALL_TIMEOUT = float(sys.argv[3])
with sqlite3.connect(sys.argv[1]) as source:
    source.execute("CREATE TABLE sentinel (value TEXT)")
    source.execute("INSERT INTO sentinel VALUES ('committed source')")
    source.commit()
    source.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    report = asyncio.run(live.run({
        "TWS_LIVE_PROXY": "http://invented-proxy.invalid:8080",
        "TWS_LIVE_DB": sys.argv[1],
    }))
    elapsed = time.monotonic() - started
    source.rollback()
    assert source.execute("SELECT value FROM sentinel").fetchall() == [("committed source",)]
    print(json.dumps({"report": report, "elapsed": elapsed}))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path / "locked.db"),
            str(operation_timeout),
            str(overall_timeout),
        ],
        cwd=live.ROOT,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["report"]["status"] == "fail"
    assert outcome["report"]["category"] == "timeout"
    assert outcome["report"]["error_class"] == "ProbeFailure"
    assert outcome["report"]["counts"]["http_requests"] == 0
    assert outcome["elapsed"] < 0.75


def test_cli_missing_configuration_writes_sanitized_report_and_exits_nonzero(tmp_path):
    import os

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("TWS_", "ADANOS_"))
    }
    target = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(Path(live.__file__)), "--report", str(target)],
        env=environment,
        capture_output=True,
        text=True,
    )
    report = json.loads(target.read_text())
    assert result.returncode == 1 and report["category"] == "configuration"
    assert json.loads(result.stdout) == report
    assert result.stderr == ""
