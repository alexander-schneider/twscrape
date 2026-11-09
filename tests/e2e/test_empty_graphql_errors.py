from datetime import datetime, timezone

import pytest

from tests.http_replay import page, tweet
from twscrape import ServiceUnavailableError
from twscrape.utils import utc

pytestmark = pytest.mark.asyncio(loop_scope="session")


def nested_search_error(empty):
    return {
        "data": {"search_by_raw_query": {"search_timeline": empty}},
        "errors": [
            {
                "message": "Internal server error",
                "path": ["search_by_raw_query", "search_timeline"],
                "locations": [{"line": 1, "column": 1}],
            }
        ],
    }


@pytest.mark.parametrize("empty", [None, {}])
async def test_empty_search_error_retries_to_actual_results(pipeline, empty):
    wire, api = pipeline
    wire.enqueue(nested_search_error(empty), page(tweet(911)))

    assert [item.id async for item in api.search("example")] == [911]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-0"]
    account = await api.pool.get("offline-0")
    assert account is not None and account.active and account.locks == {}
    assert [item["total_req"] for item in await api.pool.accounts_info()] == [1]


async def test_persistent_empty_search_error_raises_after_prior_results(pipeline):
    wire, api = pipeline
    wire.enqueue(page(tweet(912), cursor="next"))
    for _ in range(3):
        wire.enqueue(nested_search_error({}))

    received = []
    with pytest.raises(ServiceUnavailableError, match="Internal server error"):
        async for item in api.search("example"):
            received.append(item.id)

    assert received == [912]
    assert len(wire.requests) == 4
    account = await api.pool.get("offline-0")
    assert account is not None and account.active and account.locks == {}
    assert [item["total_req"] for item in await api.pool.accounts_info()] == [1]


@pytest.mark.parametrize("ids", [[], [913]])
async def test_explicit_timeline_results_survive_server_warning(pipeline, ids):
    wire, api = pipeline
    body = {
        **page(*(tweet(tid) for tid in ids)),
        "errors": [{"message": "Internal server error"}],
    }
    wire.enqueue(body)

    assert [item.id async for item in api.search("example")] == ids
    assert len(wire.requests) == 1
    account = await api.pool.get("offline-0")
    assert account is not None and account.active and account.locks == {}
    assert [item["total_req"] for item in await api.pool.accounts_info()] == [1]


async def add_second_account(api):
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )


async def test_empty_search_error_moves_to_a_free_account(pipeline, monkeypatch):
    wire, api = pipeline
    now = utc.ts()
    monkeypatch.setattr(utc, "ts", staticmethod(lambda: now))
    await add_second_account(api)
    wire.enqueue(nested_search_error({}), page(tweet(914)))

    assert [item.id async for item in api.search("example")] == [914]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-1"]
    restricted = await api.pool.get("offline-0")
    assert restricted is not None and restricted.active
    assert restricted.locks == {"SearchTimeline": datetime.fromtimestamp(now + 300, timezone.utc)}
    assert (await api.pool.get("offline-1")).locks == {}

    wire.enqueue(page(tweet(915)))
    assert [item.id async for item in api.search("example")] == [915]
    assert wire.requests[-1]["dummy_account"] == "offline-1"
    wire.enqueue(page(tweet(916, parent=915)), operation="TweetDetail")
    assert [item.id async for item in api.tweet_replies(915)] == [916]
    assert wire.requests[-1]["dummy_account"] == "offline-0"


async def test_empty_search_error_on_every_account_still_raises_after_retry_limit(pipeline):
    wire, api = pipeline
    await add_second_account(api)
    wire.enqueue(*(nested_search_error({}) for _ in range(3)))

    with pytest.raises(ServiceUnavailableError, match="Internal server error"):
        async for _ in api.search("example"):
            pass

    assert [request["dummy_account"] for request in wire.requests] == [
        "offline-0",
        "offline-1",
        "offline-1",
    ]
    assert (await api.pool.get("offline-0")).locks == {}
    assert (await api.pool.get("offline-1")).locks == {}


async def test_reply_error_retries_the_same_account_without_holding_it(pipeline):
    wire, api = pipeline
    await add_second_account(api)
    deadline = {
        "data": {"threaded_conversation_with_injections_v2": {}},
        "errors": [{"code": -1, "message": "DeadlineExceeded: Unspecified"}],
    }
    wire.enqueue(deadline, page(tweet(931, parent=930)), operation="TweetDetail")

    assert [item.id async for item in api.tweet_replies(930)] == [931]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-0"]
    assert (await api.pool.get("offline-0")).locks == {}
