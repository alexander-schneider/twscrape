import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import check_live as live
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


@pytest.mark.parametrize(
    "updates",
    [
        {"TWS_LIVE_PROXY": ""},
        {"TWS_LIVE_PROXY": "invalid"},
        {"TWS_LIVE_USERNAME": ""},
        {"TWS_LIVE_COOKIES": "auth_token=; ct0=nonempty"},
        {"TWS_LIVE_COOKIES": "auth_token=nonempty; ct0="},
        {"TWS_LIVE_COOKIES": ""},
        {"TWS_LIVE_COOKIES": "invalid-cookie-payload"},
    ],
)
async def test_missing_live_inputs_fail_without_http(monkeypatch, updates):
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid configuration must not make HTTP requests")

    monkeypatch.setattr(live.httpx.AsyncClient, "send", forbidden)
    report = await live.run(ENV | updates)
    assert report["status"] == "fail" and report["category"] == "configuration"
    assert report["counts"]["http_requests"] == 0


@pytest.mark.parametrize("query", [None, "", "  "])
async def test_empty_query_fails_before_http(monkeypatch, query):
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid query must not make HTTP requests")

    monkeypatch.setattr(live.httpx.AsyncClient, "send", forbidden)
    report = await live.run(ENV, query=query)
    assert report["status"] == "fail" and report["category"] == "configuration"


@pytest.mark.parametrize("query", [live.DEFAULT_QUERY, "a neutral custom query"])
async def test_positive_probe_uses_public_pipeline_and_actual_direct_replies(probe_wire, query):
    probe_wire.enqueue(page(tweet(101)))
    probe_wire.enqueue(
        page(tweet(102, parent=101), tweet(103, parent=999)), operation="TweetDetail"
    )
    report = await live.run(ENV, query=query)
    assert report["mode"] == "live" and report["status"] == "pass"
    assert report["candidate_import"] == "twscrape/__init__.py"
    assert report["category"] == "accepted"
    assert report["counts"] == {
        "http_requests": 2,
        "search_tweets": 1,
        "reply_parents": 1,
        "direct_replies": 1,
    }
    assert probe_wire.requests[0]["payload"]["variables"]["rawQuery"] == query
    assert probe_wire.requests[1]["payload"]["variables"]["focalTweetId"] == "101"
    serialized = json.dumps(report)
    assert all(
        value not in serialized
        for value in (
            "dummy-offline-0",
            "dummy-csrf",
            "invented-proxy",
            "offline-0",
            "Example post",
            "example_reader",
            query,
        )
    )


@pytest.mark.parametrize("profile", [None, {"id": 42, "core": {"screen_name": "partial_reader"}}])
def test_live_accepts_public_parser_fallback_for_incomplete_profiles(profile):
    parsed = list(parse_tweets(page(tweet(105, profile=profile))))
    assert len(parsed) == 1 and parsed[0].user.id == 42
    live.validate_tweet(parsed[0])


@pytest.mark.parametrize(
    "case,category",
    [
        ("search_empty", "empty_search"),
        ("replies_empty", "empty_replies"),
        ("no_parent", "no_reply_parent"),
        ("reply_error", "graphql_response"),
        ("indirect_replies", "empty_replies"),
    ],
)
async def test_empty_and_swallowed_reply_errors_cannot_pass(probe_wire, case, category):
    if case == "search_empty":
        probe_wire.enqueue(page())
    else:
        probe_wire.enqueue(page(tweet(201, reply_count=0 if case == "no_parent" else 2)))
        if case == "replies_empty":
            probe_wire.enqueue(page(), operation="TweetDetail")
        elif case == "reply_error":
            probe_wire.enqueue(
                error_body("Synthetic server problem", code=999), operation="TweetDetail"
            )
        elif case == "indirect_replies":
            probe_wire.enqueue(page(tweet(202, parent=999)), operation="TweetDetail")
    report = await live.run(ENV)
    assert report["status"] == "fail" and report["category"] == category
    assert report["counts"]["direct_replies"] == 0


async def test_request_budget_stops_actual_http_before_budget_is_exceeded(probe_wire, monkeypatch):
    monkeypatch.setattr(live, "HTTP_BUDGET", 2)
    probe_wire.enqueue(page(tweet(301), cursor="one"), page(tweet(302), cursor="two"))
    report = await live.run(ENV)
    assert report["status"] == "fail" and report["category"] == "request_budget"
    assert len(probe_wire.requests) == 2
    assert report["counts"]["http_requests"] == 2


@pytest.mark.parametrize("timeout_name", ["OPERATION_TIMEOUT", "OVERALL_TIMEOUT"])
async def test_deadline_cancels_real_http_pipeline(probe_wire, monkeypatch, timeout_name):
    monkeypatch.setattr(live, timeout_name, 0.03)
    probe_wire.enqueue(page(tweet(401)))
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
async def test_swallowed_parser_and_http_failures_cannot_pass(
    probe_wire, case, category, capsys, monkeypatch, tmp_path
):
    monkeypatch.setattr(live.models, "PARSE_ERROR_DUMP_DIR", str(tmp_path / "dumps"))
    if case == "parse":
        probe_wire.enqueue(page(tweet(501, date="invalid-date")))
    else:
        probe_wire.enqueue(page(), status=503)
    report = await live.run(ENV)
    assert report["status"] == "fail" and report["category"] == category
    assert report["counts"]["direct_replies"] == 0
    assert not (tmp_path / "dumps").exists()
    assert capsys.readouterr() == ("", "")


async def test_placeholder_signing_cannot_pass(probe_wire, monkeypatch):
    class PlaceholderSigner:
        def calc(self, *args):
            return "mocked-clid"

    async def get(*args, **kwargs):
        return PlaceholderSigner()

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(get))
    report = await live.run(ENV)
    assert report["status"] == "fail" and report["category"] == "signing"
    assert 0 < report["counts"]["http_requests"] <= live.HTTP_BUDGET
    assert probe_wire.requests == []


@pytest.mark.parametrize(
    "updates",
    [
        {"id": 0},
        {"id_str": "wrong"},
        {"rawContent": None},
        {"date": datetime(2026, 1, 1)},
        {"date": datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))},
        {"likeCount": -1},
        {"viewCount": -1},
    ],
)
def test_public_model_contract_rejects_invalid_values(updates):
    parsed = next(parse_tweets(page(tweet(601))))
    with pytest.raises(live.ProbeFailure, match="model_contract"):
        live.validate_tweet(replace(parsed, **updates))


def test_model_contract_uses_actual_parent_and_rejects_duplicate_ids():
    parsed = next(parse_tweets(page(tweet(602, parent=601))))
    live.validate_tweet(parsed, parent=601)
    with pytest.raises(live.ProbeFailure, match="direct_reply_contract"):
        live.validate_tweet(parsed, parent=999)
    with pytest.raises(live.ProbeFailure, match="duplicate_ids"):
        live.validate_tweets([parsed, parsed])


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
def test_exclusively_locked_sqlite_seed_fails_within_deadline(
    tmp_path, operation_timeout, overall_timeout
):
    code = """
import asyncio
import json
import sqlite3
import sys
import time
from scripts import check_live as live

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
    assert outcome["report"]["status"] == "fail" and outcome["report"]["category"] == "timeout"
    assert outcome["report"]["error_class"] == "ProbeFailure"
    assert outcome["report"]["counts"]["http_requests"] == 0
    assert outcome["elapsed"] < 0.75


def test_cli_missing_configuration_writes_safe_report_and_exits_nonzero(tmp_path):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TWS_")}
    target = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(Path(live.__file__)), "--report", str(target)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    report = json.loads(target.read_text())
    assert result.returncode == 1 and report["category"] == "configuration"
    assert json.loads(result.stdout) == report
    assert result.stderr == ""
