import asyncio
import json
import sys
from contextlib import aclosing

import pytest

import twscrape.cli as cli
from tests.http_replay import error_body, page, tweet
from twscrape.queue_client import AbortReqError, XClIdGenStore
from twscrape.xclid import XClIdParseError

pytestmark = pytest.mark.asyncio(loop_scope="session")


def enqueue_abort(wire, operation, status):
    if status == 404:
        for _ in range(3):
            wire.enqueue({}, operation=operation, status=404)
    else:
        wire.enqueue(error_body("Dependency: Internal error", code=131), operation=operation)


@pytest.mark.parametrize("status", [200, 404])
@pytest.mark.parametrize(
    "method,arg,operation",
    [
        ("user_by_id", 42, "UserByRestId"),
        ("user_by_id_raw", 42, "UserByRestId"),
        ("user_by_login", "example_reader", "UserByScreenName"),
        ("user_by_login_raw", "example_reader", "UserByScreenName"),
        ("tweet_details", 901, "TweetDetail"),
        ("tweet_details_raw", 901, "TweetDetail"),
    ],
)
async def test_lookup_aborts_raise_instead_of_returning_not_found(
    pipeline, method, arg, operation, status
):
    wire, api = pipeline
    enqueue_abort(wire, operation, status)

    with pytest.raises(AbortReqError):
        await getattr(api, method)(arg)

    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.active and saved.locks == {} and saved.error_msg is None
    assert len(wire.requests) == (3 if status == 404 else 1)
    assert [item["total_req"] for item in await api.pool.accounts_info()] == [0]


@pytest.mark.parametrize("status", [200, 404])
@pytest.mark.parametrize(
    "method,arg,operation,cursor_type,raw",
    [
        ("search", "example", "SearchTimeline", "Bottom", False),
        ("search_raw", "example", "SearchTimeline", "Bottom", True),
        ("tweet_replies", 900, "TweetDetail", "ShowMoreThreads", False),
        ("tweet_replies_raw", 900, "TweetDetail", "ShowMoreThreads", True),
    ],
)
async def test_page_abort_reaches_reader_after_yielded_results(
    pipeline, method, arg, operation, cursor_type, raw, status
):
    wire, api = pipeline
    wire.enqueue(
        page(tweet(901, parent=900), cursor="next", cursor_type=cursor_type),
        operation=operation,
    )
    enqueue_abort(wire, operation, status)

    received = []
    with pytest.raises(AbortReqError):
        async for item in getattr(api, method)(arg):
            received.append(item.status_code if raw else item.id)

    assert received == ([200] if raw else [901])
    assert wire.requests[1]["payload"]["variables"]["cursor"] == "next"
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.active and saved.locks == {}
    assert [item["total_req"] for item in await api.pool.accounts_info()] == [1]


def fail_signing(monkeypatch):
    cause = ValueError("Unrecognized synthetic signing bundle")
    error = XClIdParseError("Signing script not found")

    async def signing_failure(*args, **kwargs):
        raise error from cause

    monkeypatch.setattr(XClIdGenStore, "get", signing_failure)
    return error, cause


@pytest.mark.parametrize("after_page", [False, True])
async def test_signing_failure_preserves_cause_and_account_after_partial_replies(
    pipeline, monkeypatch, after_page
):
    wire, api = pipeline
    async with aclosing(api.tweet_replies(900)) as replies:
        if after_page:
            wire.enqueue(
                page(tweet(901, parent=900), cursor="next", cursor_type="ShowMoreThreads"),
                operation="TweetDetail",
            )
            assert (await anext(replies)).id == 901
        error, cause = fail_signing(monkeypatch)

        with pytest.raises(XClIdParseError) as caught:
            await anext(replies)

    assert caught.value is error
    assert caught.value.__cause__ is cause
    assert len(wire.requests) == int(after_page)
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.active and saved.locks == {} and saved.error_msg is None


@pytest.mark.parametrize("command", ["search", "user_by_login"])
@pytest.mark.parametrize("failure", ["dependency", "signing"])
async def test_cli_request_failure_exits_one_without_not_found_output(
    pipeline, monkeypatch, tmp_path, capsys, command, failure
):
    wire, _api = pipeline
    if failure == "signing":
        fail_signing(monkeypatch)
    else:
        operation = "SearchTimeline" if command == "search" else "UserByScreenName"
        enqueue_abort(wire, operation, 200)
    monkeypatch.setattr(
        sys, "argv", ["twscrape", "--db", str(tmp_path / "accounts.db"), command, "example"]
    )
    with pytest.raises(SystemExit) as caught:
        await asyncio.to_thread(cli.run)

    assert caught.value.code == 1
    assert capsys.readouterr().out == ""


async def test_cli_keeps_printed_page_but_exits_one_on_later_abort(
    pipeline, monkeypatch, tmp_path, capsys
):
    wire, _api = pipeline
    wire.enqueue(page(tweet(901), cursor="next"))
    enqueue_abort(wire, "SearchTimeline", 200)
    monkeypatch.setattr(
        sys, "argv", ["twscrape", "--db", str(tmp_path / "accounts.db"), "search", "example"]
    )
    with pytest.raises(SystemExit) as caught:
        await asyncio.to_thread(cli.run)

    assert caught.value.code == 1
    assert [json.loads(line)["id"] for line in capsys.readouterr().out.splitlines()] == [901]


@pytest.mark.parametrize(
    "method,arg,operation,body",
    [
        ("user_by_id", 42, "UserByRestId", {"data": {"user": {}}}),
        ("user_by_login", "missing", "UserByScreenName", {"data": {}}),
        (
            "tweet_details",
            900,
            "TweetDetail",
            error_body("_Missing: No status found with that ID"),
        ),
    ],
)
async def test_genuine_not_found_still_returns_none(pipeline, method, arg, operation, body):
    wire, api = pipeline
    wire.enqueue(body, operation=operation)

    assert await getattr(api, method)(arg) is None


async def test_genuine_empty_search_and_unavailable_user_timeline_still_end(pipeline):
    wire, api = pipeline
    wire.enqueue(page())
    assert [item async for item in api.search("no matches")] == []
    wire.enqueue(
        {"data": {"user": {"result": {"__typename": "UserUnavailable", "reason": "Suspended"}}}},
        operation="UserTweets",
    )
    assert [item async for item in api.user_tweets(42)] == []
