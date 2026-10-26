from contextlib import aclosing
from datetime import datetime, timezone

import pytest

from tests.http_replay import error_body, page, tweet
from twscrape.models import ParseDriftError

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_reader_searches_unique_posts_and_fetches_direct_replies(pipeline):
    wire, api = pipeline
    assert [
        (item["username"], item["active"], item["logged_in"], item["total_req"])
        for item in await api.pool.accounts_info()
    ] == [("offline-0", True, True, 0)]
    partial = {"id": 43, "core": {"screen_name": "partial_reader"}}
    wire.enqueue(
        page(tweet(101, text="Hello from an invented reader"), cursor="next"),
        page(
            tweet(101), tweet(102, uid="43", profile=partial), tweet(103, uid="44", profile=None)
        ),
    )
    documents = [item async for item in api.search("hello lang:en", limit=3)]
    assert [item.id for item in documents] == [101, 102, 103]
    first = documents[0]
    assert (
        first.rawContent,
        first.date,
        first.lang,
        first.likeCount,
        first.retweetCount,
        first.replyCount,
        first.viewCount,
    ) == (
        "Hello from an invented reader",
        datetime(2026, 10, 24, 12, tzinfo=timezone.utc),
        "en",
        7,
        3,
        2,
        1234,
    )
    assert (
        first.user.id,
        first.user.username,
        first.user.displayname,
        first.user.rawDescription,
        first.user.followersCount,
        first.user.profileImageUrl,
    ) == (
        42,
        "example_reader",
        "Example Reader",
        "An invented profile",
        321,
        "https://images.invalid/42.jpg",
    )
    assert [(item.user.id, item.user.username) for item in documents[1:]] == [
        (43, "partial_reader"),
        (44, ""),
    ]
    wire.enqueue(page(tweet(104, text="An invented answer", parent=101)), operation="TweetDetail")
    replies = [item async for item in api.tweet_replies(first.id)]
    assert [(item.id, item.rawContent, item.inReplyToTweetId) for item in replies] == [
        (104, "An invented answer", 101)
    ]
    assert [request["operation"] for request in wire.requests] == [
        "SearchTimeline",
        "SearchTimeline",
        "TweetDetail",
    ]
    assert wire.requests[0]["payload"]["variables"]["rawQuery"] == "hello lang:en"
    assert wire.requests[1]["payload"]["variables"]["cursor"] == "next"
    assert all(r["signed"] and r["csrf_set"] and r["authorized"] for r in wire.requests)
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.locks == {}


async def test_direct_reply_pagination_filters_nested_and_unrelated(pipeline):
    wire, api = pipeline
    wire.enqueue(
        page(
            tweet(200),
            tweet(201, parent=200),
            tweet(202, parent=201),
            tweet(203, parent=999),
            cursor="more",
            cursor_type="ShowMoreThreads",
        ),
        page(tweet(204, parent=200)),
        operation="TweetDetail",
    )
    replies = [item async for item in api.tweet_replies(200, limit=20)]
    assert [(item.id, item.inReplyToTweetId) for item in replies] == [(201, 200), (204, 200)]
    assert wire.requests[0]["payload"]["variables"]["focalTweetId"] == "200"
    assert wire.requests[1]["payload"]["variables"]["cursor"] == "more"


async def test_search_no_progress_is_bounded_across_changing_raw_entries(pipeline):
    wire, api = pipeline
    for index in range(4):
        body = page(tweet(301), cursor=f"next-{index}")
        body["data"]["timeline"]["instructions"][0]["entries"][0]["entryId"] = f"different-{index}"
        wire.enqueue(body)
    assert [item.id async for item in api.search("hello", limit=2)] == [301]
    assert len(wire.requests) == 4


async def test_early_reader_close_releases_sqlite_queue_lock(pipeline):
    wire, api = pipeline
    wire.enqueue(page(tweet(401), cursor="unused"))
    async with aclosing(api.search("hello", limit=2)) as generator:
        assert (await anext(generator)).id == 401
    await wire.real_sleep(0.05)
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.locks == {}
    assert len(wire.requests) == 1


async def test_search_recovers_from_transient_error_on_same_account(pipeline):
    wire, api = pipeline
    wire.enqueue(error_body("ServiceUnavailable"), page(tweet(501, text="Recovered example")))
    assert [(item.id, item.rawContent) async for item in api.search("example")] == [
        (501, "Recovered example")
    ]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-0"]
    assert [(item["active"], item["total_req"]) for item in await api.pool.accounts_info()] == [
        (True, 1)
    ]


async def test_search_rotates_expired_session_and_reports_account_state(pipeline):
    wire, api = pipeline
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )
    wire.enqueue(error_body("Could not authenticate you", code=32), page(tweet(601)))
    assert [item.id async for item in api.search("example")] == [601]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-1"]
    assert {
        item["username"]: (item["active"], item["total_req"], item["error_msg"])
        for item in await api.pool.accounts_info()
    } == {
        "offline-0": (False, 0, "(32) Could not authenticate you"),
        "offline-1": (True, 1, "None"),
    }


async def test_ambiguous_forbidden_rotates_search_but_keeps_account_for_replies(pipeline):
    wire, api = pipeline
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )
    wire.enqueue({}, status=403)
    wire.enqueue(page(tweet(701)))
    assert [item.id async for item in api.search("example")] == [701]
    wire.enqueue(page(tweet(702, parent=701)), operation="TweetDetail")
    assert [item.id async for item in api.tweet_replies(701)] == [702]
    assert [(r["operation"], r["dummy_account"]) for r in wire.requests] == [
        ("SearchTimeline", "offline-0"),
        ("SearchTimeline", "offline-1"),
        ("TweetDetail", "offline-0"),
    ]
    assert {item["username"]: item["active"] for item in await api.pool.accounts_info()} == {
        "offline-0": True,
        "offline-1": True,
    }


async def test_parser_error_reaches_reader_after_already_yielded_posts(pipeline):
    wire, api = pipeline
    malformed = [tweet(tid, date="not a timestamp") for tid in range(802, 812)]
    wire.enqueue(page(tweet(801, text="Readable example"), cursor="next"), page(*malformed))
    received = []
    with pytest.raises(ParseDriftError):
        async for item in api.search("example"):
            received.append((item.id, item.rawContent))
    assert received == [(801, "Readable example")]
    await wire.real_sleep(0.05)
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.active and saved.locks == {}
    assert len(wire.requests) == 2
