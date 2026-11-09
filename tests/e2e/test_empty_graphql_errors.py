import pytest

from tests.http_replay import page, tweet
from twscrape import ServiceUnavailableError

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
    body = page(*(tweet(tid) for tid in ids))
    body["errors"] = [{"message": "Internal server error"}]
    wire.enqueue(body)

    assert [item.id async for item in api.search("example")] == ids
    assert len(wire.requests) == 1
    account = await api.pool.get("offline-0")
    assert account is not None and account.active and account.locks == {}
    assert [item["total_req"] for item in await api.pool.accounts_info()] == [1]
