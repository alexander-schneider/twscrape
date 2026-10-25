import json
import os
from pathlib import Path

import pytest

import twscrape.api as api_module
from twscrape.accounts_pool import NoAccountError
from twscrape.api import API
from twscrape.utils import gather, get_env_bool


class MockedError(Exception):
    pass


GQL_GEN = [
    "search",
    "tweet_replies",
    "retweeters",
    "followers",
    "following",
    "user_tweets",
    "user_tweets_and_replies",
    "list_timeline",
    "trends",
]

RAW_SEARCH_PATH = Path(__file__).parent / "mocked-data" / "raw_search.json"


class DummyResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


def load_raw_search() -> dict:
    return json.loads(RAW_SEARCH_PATH.read_text())


def test_gql_operation_ids_match_upstream_v0_20_1():
    assert {
        name: getattr(api_module, name) for name in dir(api_module) if name.startswith("OP_")
    } == {
        "OP_BlueVerifiedFollowers": "u3PkPbg--arppBcwNbF1ig/BlueVerifiedFollowers",
        "OP_Bookmarks": "iblrFnKr6PZUR-dWpfXG6g/Bookmarks",
        "OP_Followers": "JNyQdTISpzCkj_1fqxDvFg/Followers",
        "OP_Following": "qGZZDF3mp91q7X22s3HxpA/Following",
        "OP_GenericTimelineById": "ee4dBLWL8a8qg6n19m1htQ/GenericTimelineById",
        "OP_ListLatestTweetsTimeline": "1LE3u14FJjPZUHKFGzos2g/ListLatestTweetsTimeline",
        "OP_Retweeters": "ROjiuYueotTnWoI8m2YaiQ/Retweeters",
        "OP_SearchTimeline": "hyPfJYJ_XAtDYoslQc-Rgg/SearchTimeline",
        "OP_TweetDetail": "XMOz5h24KAZ86qKffKTLdQ/TweetDetail",
        "OP_UserByRestId": "xvmVfRLmnr1alc5f2dib0Q/UserByRestId",
        "OP_UserByScreenName": "Gb-d6r0vxPOADdG62OEBpQ/UserByScreenName",
        "OP_UserCreatorSubscriptions": "Qxe_gd-ZvdofnzSL8Ngzpw/UserCreatorSubscriptions",
        "OP_UserMedia": "VyudDWQnr9vJNw7GasFz2g/UserMedia",
        "OP_UserTweets": "SXVCYB8XHSS25nzIljNtZA/UserTweets",
        "OP_UserTweetsAndReplies": "qUpkZU6eN8MbtQb7rC_pYg/UserTweetsAndReplies",
    }


def make_search_page(entry_ids: list[str], cursor: str | None):
    entries = [
        {"entryId": entry_id, "content": {"entryType": "TimelineTimelineItem"}}
        for entry_id in entry_ids
    ]
    if cursor is not None:
        entries.append(
            {
                "entryId": f"cursor-bottom-{cursor}",
                "content": {
                    "entryType": "TimelineTimelineCursor",
                    "__typename": "TimelineTimelineCursor",
                    "cursorType": "Bottom",
                    "value": cursor,
                },
            }
        )

    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {
                        "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
                    }
                }
            }
        }
    }


async def test_gql_params(api_mock: API, monkeypatch):
    for func in GQL_GEN:
        args = []

        def mock_gql_items(*a, **kw):
            args.append((a, kw))
            raise MockedError()

        try:
            monkeypatch.setattr(api_mock, "_gql_items", mock_gql_items)
            await gather(getattr(api_mock, func)("user1", limit=100, kv={"count": 100}))
        except MockedError:
            pass

        assert len(args) == 1, f"{func} not called once"
        assert args[0][0][1]["count"] == 100, f"count not changed in {func}"


async def test_raise_when_no_account(api_mock: API):
    await api_mock.pool.delete_accounts(["user1"])
    assert len(await api_mock.pool.get_all()) == 0

    assert get_env_bool("TWS_RAISE_WHEN_NO_ACCOUNT") is False
    os.environ["TWS_RAISE_WHEN_NO_ACCOUNT"] = "1"
    assert get_env_bool("TWS_RAISE_WHEN_NO_ACCOUNT") is True

    with pytest.raises(NoAccountError):
        await gather(api_mock.search("foo", limit=10))

    with pytest.raises(NoAccountError):
        await api_mock.user_by_id(123)

    del os.environ["TWS_RAISE_WHEN_NO_ACCOUNT"]
    assert get_env_bool("TWS_RAISE_WHEN_NO_ACCOUNT") is False


def test_get_entries_ignores_malformed_timeline_entries(api_mock: API):
    obj = {
        "entries": [
            None,
            "not-a-dict",
            {"entryId": "cursor-bottom-1"},
            {"entryId": "messageprompt-1"},
            {"entryId": "module-1"},
            {"entryId": "who-to-follow-1"},
            {"entryId": "tweet-1"},
            {"content": {"entryType": "TimelineTimelineItem"}},
        ]
    }

    entries = api_mock._get_entries(obj)

    assert entries == [
        {"entryId": "tweet-1"},
        {"content": {"entryType": "TimelineTimelineItem"}},
    ]


async def test_gql_items_stops_on_repeated_search_page(api_mock: API, monkeypatch):
    pages = [
        DummyResponse(make_search_page(["tweet-1", "tweet-2"], "cursor-1")),
        DummyResponse(make_search_page(["tweet-1", "tweet-2"], "cursor-2")),
        DummyResponse(make_search_page(["tweet-3", "tweet-4"], None)),
    ]
    calls = []

    class FakeQueueClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def get(self, url, params=None):
            calls.append(("get", url, params))
            raise AssertionError("SearchTimeline should use POST")

        async def post(self, url, json=None):
            calls.append(("post", url, json))
            if not pages:
                raise AssertionError("unexpected extra pagination request")
            return pages.pop(0)

    monkeypatch.setattr(api_module, "QueueClient", FakeQueueClient)

    reps = await gather(
        api_mock._gql_items(
            api_module.OP_SearchTimeline,
            {"rawQuery": "foo", "count": 20, "product": "Latest", "querySource": "typed_query"},
        )
    )

    assert len(reps) == 1
    assert len(calls) == 2
    assert all(x[0] == "post" for x in calls)


async def test_gql_items_stops_on_repeated_cursor_for_other_timelines(api_mock: API, monkeypatch):
    pages = [
        DummyResponse(make_search_page(["tweet-1"], "cursor-1")),
        DummyResponse(make_search_page(["tweet-2"], "cursor-1")),
        DummyResponse(make_search_page(["tweet-3"], None)),
    ]
    calls = []

    class FakeQueueClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def get(self, url, params=None):
            calls.append((url, params))
            if not pages:
                raise AssertionError("unexpected extra pagination request")
            return pages.pop(0)

        async def post(self, url, json=None):
            raise AssertionError("Following should use GET")

    monkeypatch.setattr(api_module, "QueueClient", FakeQueueClient)

    reps = await gather(api_mock._gql_items(api_module.OP_Following, {"userId": "123"}))

    assert len(reps) == 2
    assert len(calls) == 2


async def test_gql_items_continues_past_empty_search_pages(api_mock: API, monkeypatch):
    pages = [
        DummyResponse(make_search_page([], "cursor-1")),
        DummyResponse(make_search_page(["tweet-1", "tweet-2"], None)),
    ]
    calls = []

    class FakeQueueClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def get(self, url, params=None):
            calls.append(("get", url, params))
            raise AssertionError("SearchTimeline should use POST")

        async def post(self, url, json=None):
            calls.append(("post", url, json))
            if not pages:
                raise AssertionError("unexpected extra pagination request")
            return pages.pop(0)

    monkeypatch.setattr(api_module, "QueueClient", FakeQueueClient)

    reps = await gather(
        api_mock._gql_items(
            api_module.OP_SearchTimeline,
            {"rawQuery": "foo", "count": 20, "product": "Latest", "querySource": "typed_query"},
        )
    )

    assert len(reps) == 1
    assert len(calls) == 2
    assert all(x[0] == "post" for x in calls)


@pytest.mark.parametrize(
    ("op", "variables"),
    [
        (
            api_module.OP_SearchTimeline,
            {"rawQuery": "foo", "count": 20, "product": "Latest", "querySource": "typed_query"},
        ),
        (
            api_module.OP_Followers,
            {"userId": "2244994945", "count": 20, "includePromotedContent": False},
        ),
        (
            api_module.OP_UserTweetsAndReplies,
            {
                "userId": "2244994945",
                "count": 40,
                "includePromotedContent": True,
                "withCommunity": True,
                "withVoice": True,
                "withV2Timeline": True,
            },
        ),
    ],
)
async def test_gql_items_uses_post_for_selected_timeline_ops(
    api_mock: API, monkeypatch, op, variables
):
    calls = []

    class FakeQueueClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def get(self, url, params=None):
            calls.append(("get", url, params))
            raise AssertionError(f"{op} should use POST")

        async def post(self, url, json=None):
            calls.append(("post", url, json))
            return DummyResponse(make_search_page(["tweet-1"], None))

    monkeypatch.setattr(api_module, "QueueClient", FakeQueueClient)

    reps = await gather(api_mock._gql_items(op, variables, limit=1))

    assert len(reps) == 1
    assert calls
    assert all(x[0] == "post" for x in calls)


async def test_search_deduplicates_across_pages(api_mock: API, monkeypatch):
    expected_ids = [x.id for x in api_module.parse_tweets(load_raw_search())]

    async def mock_search_raw(*args, **kwargs):
        yield DummyResponse(load_raw_search())
        yield DummyResponse(load_raw_search())

    monkeypatch.setattr(api_mock, "search_raw", mock_search_raw)

    tweets = await gather(api_mock.search("foo", limit=15))
    tweet_ids = [x.id for x in tweets]

    assert tweet_ids == expected_ids
    assert len(tweet_ids) == len(set(tweet_ids))


async def test_user_by_id_uses_public_gql_host(api_mock: API, monkeypatch):
    calls = []

    async def mock_gql_item(*args, **kwargs):
        calls.append((args, kwargs))
        raise MockedError()

    monkeypatch.setattr(api_mock, "_gql_item", mock_gql_item)

    with pytest.raises(MockedError):
        await api_mock.user_by_id_raw(2244994945)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == api_module.OP_UserByRestId
    assert args[1]["userId"] == "2244994945"
    assert args[1]["withSafetyModeUserFields"] is True
    assert kwargs["gql_url"] == api_module.PUBLIC_GQL_URL


def parsed_search_page(ids: list[int], cursor: str | None, people=False):
    page = make_search_page([f"result-{item_id}" for item_id in ids], cursor)
    entries = page["data"]["search_by_raw_query"]["search_timeline"]["timeline"]["instructions"][
        0
    ]["entries"]
    for entry, item_id in zip(entries, ids):
        user = {
            "__typename": "User",
            "rest_id": str(item_id),
            "core": {"screen_name": f"user{item_id}", "name": f"User {item_id}"},
        }
        entry["content"]["result"] = (
            user
            if people
            else {
                "__typename": "Tweet",
                "rest_id": str(item_id),
                "legacy": {
                    "id_str": str(item_id),
                    "user_id_str": str(item_id),
                    "created_at": "Wed Jan 01 12:00:00 +0000 2025",
                    "full_text": f"Tweet {item_id}",
                },
                "core": {"user_results": {"result": user}},
            }
        )
    return page


def mock_search_pages(monkeypatch, pages):
    calls = []

    class FakeQueueClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            calls.append(json)
            assert pages, "unexpected extra pagination request"
            return DummyResponse(pages.pop(0))

    monkeypatch.setattr(api_module, "QueueClient", FakeQueueClient)
    return calls


@pytest.mark.parametrize("method", ["search", "search_user"])
async def test_parsed_search_limit_counts_unique_models(api_mock, monkeypatch, method):
    pages = [
        parsed_search_page([1, 2], "a", method == "search_user"),
        parsed_search_page([2], "b", method == "search_user"),
        parsed_search_page([3, 4], None, method == "search_user"),
    ]
    calls = mock_search_pages(monkeypatch, pages)

    items = await gather(getattr(api_mock, method)("query", limit=3))

    assert [item.id for item in items] == [1, 2, 3]
    assert len(calls) == 3
    assert calls[0]["variables"]["product"] == ("People" if method == "search_user" else "Latest")


@pytest.mark.parametrize("method", ["search", "search_user"])
async def test_parsed_search_stops_after_three_pages_without_models(api_mock, monkeypatch, method):
    calls = mock_search_pages(
        monkeypatch,
        [make_search_page([f"ignored-{index}"], str(index)) for index in range(3)],
    )

    items = await gather(getattr(api_mock, method)("query", limit=10))

    assert items == []
    assert len(calls) == 3


@pytest.mark.parametrize("method", ["search", "search_user"])
async def test_parsed_search_resets_no_progress_count(api_mock, monkeypatch, method):
    pages = [
        make_search_page(["ignored-1"], "a"),
        make_search_page(["ignored-2"], "b"),
        parsed_search_page([1], "c", method == "search_user"),
        make_search_page(["ignored-3"], "d"),
        make_search_page(["ignored-4"], "e"),
        parsed_search_page([2], None, method == "search_user"),
    ]
    calls = mock_search_pages(monkeypatch, pages)

    items = await gather(getattr(api_mock, method)("query", limit=2))

    assert [item.id for item in items] == [1, 2]
    assert len(calls) == 6


async def test_raw_search_keeps_page_based_limit(api_mock, monkeypatch):
    calls = mock_search_pages(
        monkeypatch,
        [parsed_search_page([1, 2], "a"), parsed_search_page([3], None)],
    )

    pages = await gather(api_mock.search_raw("query", limit=1))

    assert len(pages) == 1
    assert [tweet.id for tweet in api_module.parse_tweets(pages[0].json())] == [1, 2]
    assert len(calls) == 1
