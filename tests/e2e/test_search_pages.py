"""max_pages bounds the HTTP requests of one search call (app council, Opus P2).

During warm-up a paced search account sends about one request per 600 s, so a ticker search that pages three
times outlasts the app's 15-minute chunk timeout. With max_pages the caller asks for at most that many
SearchTimeline requests, counting every page request, also the cursor-only pages _gql_items follows on its own.
"""

import pytest

from tests.http_replay import page, tweet

pytestmark = pytest.mark.asyncio(loop_scope="session")


def first_page_then_cursor_only(wire, cursor_only_pages=3):
    wire.enqueue(page(tweet(941), cursor="c1"))
    for n in range(2, cursor_only_pages + 2):
        wire.enqueue(page(cursor=f"c{n}"))


@pytest.mark.parametrize("raw", [False, True])
async def test_max_pages_one_sends_exactly_one_request(pipeline, raw):
    wire, api = pipeline
    wire.enqueue(page(tweet(941), cursor="c1"))

    if raw:
        found = [rep async for rep in api.search_raw("example", max_pages=1)]
        assert len(found) == 1
    else:
        found = [item.id async for item in api.search("example", max_pages=1)]
        assert found == [941]
    assert [r["operation"] for r in wire.requests] == ["SearchTimeline"]


async def test_max_pages_counts_cursor_only_pages(pipeline):
    """The second request answers only a cursor; it still counts, so max_pages=2 stops after it."""
    wire, api = pipeline
    first_page_then_cursor_only(wire, cursor_only_pages=1)

    found = [item.id async for item in api.search("example", max_pages=2)]

    assert found == [941] and len(wire.requests) == 2


async def test_without_max_pages_paging_is_unchanged(pipeline):
    """Default None: the tweet page, then three cursor-only pages before _gql_items gives up, as before."""
    wire, api = pipeline
    first_page_then_cursor_only(wire, cursor_only_pages=3)

    found = [item.id async for item in api.search("example")]

    assert found == [941] and len(wire.requests) == 4


async def test_max_pages_keeps_the_web_client_variables(pipeline):
    wire, api = pipeline
    wire.enqueue(page(tweet(941), cursor="c1"), page(tweet(942)))

    [item async for item in api.search("example", max_pages=1)]
    [item async for item in api.search("example")]

    with_cap, without = (r["payload"] for r in wire.requests)
    assert list(with_cap["variables"]) == list(without["variables"])
    assert with_cap == without


@pytest.mark.parametrize("max_pages", [0, -1])
async def test_max_pages_must_be_positive(pipeline, max_pages):
    _, api = pipeline
    with pytest.raises(ValueError, match="max_pages"):
        [item async for item in api.search("example", max_pages=max_pages)]


@pytest.mark.parametrize("raw", [False, True])
async def test_on_page_is_called_for_every_answer_also_a_cursor_only_one(pipeline, raw):
    """api.adanos.org#3565 council round 6: a caller that stops on a cap needs to know that X answered, also when
    the answer held only a cursor, which _gql_items follows on its own without yielding it."""
    wire, api = pipeline
    wire.enqueue(page(cursor="c1"), page(cursor="c2"), page(tweet(941)))
    answers: list[int] = []

    def on_page():
        answers.append(len(wire.requests))

    if raw:
        [rep async for rep in api.search_raw("example", max_pages=3, on_page=on_page)]
    else:
        assert [item.id async for item in api.search("example", max_pages=3, on_page=on_page)] == [941]

    assert answers == [1, 2, 3]


async def test_without_on_page_the_search_is_unchanged(pipeline):
    wire, api = pipeline
    wire.enqueue(page(cursor="c1"), page(tweet(941)))

    assert [item.id async for item in api.search("example")] == [941]
    assert len(wire.requests) == 2
