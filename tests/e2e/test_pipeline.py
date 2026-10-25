from contextlib import aclosing
from datetime import datetime, timezone

import pytest

from tests.http_replay import page, tweet

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_search_pagination_authentication_and_consumed_fields(pipeline):
    wire, api = pipeline
    partial = {"id": 43, "core": {"screen_name": "partial_author"}}
    wire.enqueue(
        page(tweet(101, text="Literal $AAPL text"), cursor="next"),
        page(
            tweet(101), tweet(102, uid="43", profile=partial), tweet(103, uid="44", profile=None)
        ),
    )
    documents = [item async for item in api.search("$AAPL lang:en", limit=3)]
    assert [item.id for item in documents] == [101, 102, 103]
    first = documents[0]
    assert (
        first.rawContent,
        first.date,
        first.likeCount,
        first.retweetCount,
        first.replyCount,
        first.viewCount,
    ) == (
        "Literal $AAPL text",
        datetime(2026, 10, 24, 12, tzinfo=timezone.utc),
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
        "full_author",
        "Full Author",
        "Literal preserved bio",
        321,
        "https://images.invalid/42.jpg",
    )
    assert [(item.user.id, item.user.username) for item in documents[1:]] == [
        (43, "partial_author"),
        (44, ""),
    ]
    assert len(wire.requests) == 2
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
    assert [item.id async for item in api.search("$AAPL", limit=2)] == [301]
    assert len(wire.requests) == 4


async def test_early_consumer_close_releases_sqlite_queue_lock(pipeline):
    wire, api = pipeline
    wire.enqueue(page(tweet(401), cursor="unused"))
    async with aclosing(api.search("$AAPL", limit=1)) as generator:
        assert (await anext(generator)).id == 401
    await wire.real_sleep(0.05)
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.locks == {}
    assert len(wire.requests) == 1
