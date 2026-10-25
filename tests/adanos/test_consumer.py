from datetime import datetime, timezone

import pytest

from tests.http_replay import OLD_DATE, error_body, page, tweet

ANCHOR = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
QUERY = "$AAPL lang:en until_time:1792886400 since_time:1792800000 -filter:links"
pytestmark = [pytest.mark.adanos, pytest.mark.asyncio(loop_scope="session")]


async def test_pool_proxy_cache_and_sqlite_roundtrip(consumer, wire, tmp_path, monkeypatch):
    monkeypatch.setattr(consumer.settings, "tws_proxy", "")
    no_proxy = consumer.XAccountPool(str(tmp_path / "no-proxy.db"))
    with pytest.raises(consumer.XAccountPoolError, match="TWS_PROXY not configured"):
        await no_proxy.get_api()
    assert no_proxy._api is None
    assert not (tmp_path / "no-proxy.db").exists()
    monkeypatch.setattr(
        consumer.settings, "tws_proxy", "http://offline-user:invented-secret@offline.invalid:8080"
    )
    wrapper, api, _ = await wire.consumer(tmp_path, accounts=2)
    assert api is await wrapper.get_api()
    assert api.pool._order_by == "RANDOM()"
    await api.pool.add_account(
        "offline-inactive", "_", "_", "_", user_agent="offline-verification/1.0"
    )
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.username == "offline-0" and saved.active
    assert saved.cookies == {"auth_token": "dummy-offline-0", "ct0": "dummy-csrf-offline-0"}
    assert await wrapper.get_active_count() == 2
    await api.pool.mark_inactive("offline-1", "invented offline state")
    assert await wrapper.get_active_count() == 1
    assert "invented-secret" not in str(wire.logs.records)
    assert wire.requests == []


async def test_search_pagination_known_day_cutoff_and_partial_profiles(consumer, wire, tmp_path):
    _, _, scraper = await wire.consumer(tmp_path)
    partial = {"id": 42, "core": {"screen_name": "partial_author"}}
    new = tweet(103, text="Literal $AAPL partial author", profile=partial)
    missing = tweet(104, text="Literal $AAPL profile absent", uid="43", profile=None)
    wire.enqueue(
        page(tweet(101, date=OLD_DATE, uid="91"), tweet(102, uid="92"), new, cursor="page-two"),
        page(new, missing),
    )
    outcome = await scraper.search_cashtag("AAPL", ANCHOR, limit=2, known_ids={"102"})
    assert [t["tweet_id"] for t in outcome.tweets] == ["103", "104"]
    assert [t["tweet_id"] for t in outcome.known_parent_tweets] == ["102"]
    assert outcome.known_parent_tweets[0]["context_only"] is True
    assert outcome.skipped_known == 1 and outcome.skipped_before_day_start == 1
    assert outcome.attempts == 1 and not outcome.degraded
    assert outcome.tweets[0] == {
        "tweet_id": "103",
        "author_id": "42",
        "author_username": "partial_author",
        "text": "Literal $AAPL partial author",
        "likes": 7,
        "retweets": 3,
        "replies_count": 2,
        "views": 1234,
        "created_at": ANCHOR,
        "is_reply": False,
        "parent_tweet_id": None,
    }
    assert outcome.tweets[1]["author_id"] == "43" and outcome.tweets[1]["author_username"] == ""
    assert len(wire.requests) == 2
    assert all(r["payload"]["variables"]["rawQuery"] == QUERY for r in wire.requests)
    assert wire.requests[1]["payload"]["variables"]["cursor"] == "page-two"
    assert all(r["signed"] and r["csrf_set"] and r["authorized"] for r in wire.requests)


async def test_real_model_retains_rich_profile_over_partial_copy(consumer, wire, tmp_path):
    _, api, scraper = await wire.consumer(tmp_path)
    wire.enqueue(
        page(
            tweet(110),
            shadows=[
                {"__typename": "User", "rest_id": "42", "core": {"screen_name": "partial_author"}}
            ],
        )
    )
    documents = [doc async for doc in api.search("$AAPL", limit=1)]
    assert len(documents) == 1
    user = documents[0].user
    assert (
        user.id,
        user.username,
        user.displayname,
        user.rawDescription,
        user.followersCount,
        user.profileImageUrl,
    ) == (
        42,
        "full_author",
        "Full Author",
        "Literal preserved bio",
        321,
        "https://images.invalid/42.jpg",
    )
    assert scraper._normalize_tweet(documents[0])["author_username"] == "full_author"


async def test_zero_limit_does_not_query_or_initialize_api(consumer, wire, tmp_path):
    wrapper = consumer.XAccountPool(str(tmp_path / "unused.db"))
    scraper = consumer.XTweetScraper(wrapper)
    outcome = await scraper.search_cashtag("AAPL", ANCHOR, limit=0)
    assert outcome.tweets == [] and outcome.attempts == 0 and outcome.effective_limit == 0
    assert wrapper._api is None and wire.requests == []


async def test_direct_replies_traverse_real_tweetdetail_pages(consumer, wire, tmp_path):
    _, _, scraper = await wire.consumer(tmp_path)
    wire.enqueue(
        page(
            tweet(200),
            tweet(201, text="Literal direct reply", parent=200),
            tweet(202, parent=201),
            tweet(203, parent=999),
            cursor="more-replies",
            cursor_type="ShowMoreThreads",
        ),
        page(tweet(204, text="Literal second direct reply", parent=200)),
        operation="TweetDetail",
    )
    replies = await scraper.fetch_replies("200", limit=20)
    assert [r["tweet_id"] for r in replies] == ["201", "204"]
    assert all(r["is_reply"] and r["parent_tweet_id"] == "200" for r in replies)
    assert replies[0]["text"] == "Literal direct reply" and replies[0]["likes"] == 7
    assert wire.requests[0]["payload"]["variables"]["focalTweetId"] == "200"
    assert wire.requests[1]["payload"]["variables"]["cursor"] == "more-replies"


async def test_direct_reply_partial_result_is_nonfatal_after_graphql_error(
    consumer, wire, tmp_path
):
    _, _, scraper = await wire.consumer(tmp_path)
    wire.enqueue(
        page(tweet(211, parent=210), cursor="reply-failure", cursor_type="ShowMoreThreads"),
        error_body("Synthetic nonretryable server problem", code=999),
        operation="TweetDetail",
    )
    replies = await scraper.fetch_replies("210", limit=20)
    assert [r["tweet_id"] for r in replies] == ["211"]
    assert replies[0]["is_reply"] and replies[0]["parent_tweet_id"] == "210"
    assert len(wire.requests) == 2


async def test_scrape_ticker_known_replies_cutoff_and_budget(consumer, wire, tmp_path):
    _, _, scraper = await wire.consumer(tmp_path)
    wire.enqueue(page(tweet(300), tweet(301), tweet(302)))
    wire.enqueue(
        page(
            tweet(401, parent=301),
            tweet(402, parent=301),
            tweet(403, parent=301, date=OLD_DATE),
            tweet(404, parent=401),
        ),
        operation="TweetDetail",
    )
    budget = consumer.ReplyFetchBudget(max_fetches=1)
    result, tweets = await scraper.scrape_ticker(
        "AAPL", ANCHOR, known_ids={"300", "402"}, reply_budget=budget
    )
    assert [t["tweet_id"] for t in tweets] == ["401", "300", "301", "302"]
    assert all(t["ticker"] == "AAPL" for t in tweets)
    assert tweets[1]["context_only"] is True
    assert result.tweets_found == 2 and result.replies_found == 1
    assert result.skipped_known == 1 and result.skipped_known_replies == 1
    assert result.reply_fetches_attempted == 1 and result.reply_fetches_skipped_budget == 2
    assert result.tickers_scraped == 1 and result.tickers_failed == result.tickers_partial == 0
    assert (budget.fetches_attempted, budget.fetches_skipped) == (1, 2)
    assert len(wire.requests) == 2


async def test_transient_queue_retries_then_consumer_recovers_unique_target(
    consumer, wire, tmp_path
):
    _, _, scraper = await wire.consumer(tmp_path)
    wire.enqueue(
        page(tweet(501), cursor="temporary-failure"),
        *[error_body("ServiceUnavailable")] * 3,
        page(tweet(501), tweet(502), tweet(503)),
    )
    outcome = await scraper.search_cashtag("AAPL", ANCHOR, limit=3)
    assert [t["tweet_id"] for t in outcome.tweets] == ["501", "502", "503"]
    assert outcome.attempts == 2 and outcome.transient_retries == 1
    assert outcome.service_unavailable_count == 1 and outcome.deadline_exceeded_count == 0
    assert outcome.skipped_retry == 1 and not outcome.degraded
    assert len(wire.requests) == 5
    assert "cursor" not in wire.requests[-1]["payload"]["variables"]


async def test_partial_deadline_salvage_exposed_by_scrape_result(consumer, wire, tmp_path):
    _, _, scraper = await wire.consumer(tmp_path)
    for attempt in range(2):
        wire.enqueue(
            page(tweet(601), cursor=f"failure-{attempt}"), *[error_body("DeadlineExceeded")] * 3
        )
    result, tweets = await scraper.scrape_ticker(
        "AAPL", ANCHOR, reply_budget=consumer.ReplyFetchBudget(max_fetches=0)
    )
    assert [t["tweet_id"] for t in tweets] == ["601"]
    assert result.tweets_found == 1 and result.tickers_scraped == 1
    assert result.tickers_partial == result.partial_salvages == 1 and result.tickers_failed == 0
    assert result.transient_retries == 1 and result.deadline_exceeded_count == 2
    assert result.reply_fetches_attempted == 0 and result.reply_fetches_skipped_budget == 1
    assert "partial search salvage 1/2" in result.errors[0]
    assert len(wire.requests) == 8


@pytest.mark.parametrize("has_valid", [True, False])
async def test_parser_drift_actual_exception_and_python_log_bridge(
    consumer, wire, tmp_path, has_valid
):
    _, _, scraper = await wire.consumer(tmp_path)
    invalid = [tweet(tid, date="not-a-date") for tid in range(710, 720)]
    wire.enqueue(page(*([tweet(701)] if has_valid else []), *invalid))
    result, tweets = await scraper.scrape_ticker(
        "AAPL", ANCHOR, reply_budget=consumer.ReplyFetchBudget(max_fetches=0)
    )
    if has_valid:
        assert [t["tweet_id"] for t in tweets] == ["701"]
        assert result.tickers_partial == result.partial_salvages == result.tickers_scraped == 1
        assert result.tickers_failed == 0 and "ParseDriftError" in result.errors[0]
    else:
        assert tweets == [] and result.tickers_failed == 1 and result.tickers_scraped == 0
        assert "Aborting tweet parsing after 10 item failures" in result.errors[0]
    assert result.transient_retries == 0 and len(wire.requests) == 1
    parse_logs = [
        r
        for r in wire.logs.records
        if r["message"].startswith("Failed to parse response of tweet")
    ]
    assert len(parse_logs) == 10
    assert all(r["name"] == "twscrape.models" and r["level"] == "WARNING" for r in parse_logs)


async def test_code32_rotates_sqlite_accounts_and_updates_wrapper_active_count(
    consumer, wire, tmp_path
):
    wrapper, api, scraper = await wire.consumer(tmp_path, accounts=2)
    wire.enqueue(error_body("Could not authenticate you", code=32), page(tweet(801)))
    outcome = await scraper.search_cashtag("AAPL", ANCHOR, limit=1)
    assert [t["tweet_id"] for t in outcome.tweets] == ["801"]
    assert len(wire.requests) == 2
    assert len({r["dummy_account"] for r in wire.requests}) == 2
    assert await wrapper.get_active_count() == 1
    accounts = await api.pool.accounts_info()
    assert sum(not a["active"] for a in accounts) == 1
    assert outcome.attempts == 1 and outcome.transient_retries == 0


async def test_search_stops_after_three_pages_without_new_parsed_ids(consumer, wire, tmp_path):
    _, _, scraper = await wire.consumer(tmp_path)
    wire.enqueue(page(tweet(901), cursor="no-progress-0"))
    for index in range(1, 4):
        body = page(tweet(901), cursor=f"no-progress-{index}")
        body["data"]["timeline"]["instructions"][0]["entries"][0]["entryId"] = (
            f"search-result-{index}"
        )
        wire.enqueue(body)
    outcome = await scraper.search_cashtag("AAPL", ANCHOR, limit=2)
    assert [t["tweet_id"] for t in outcome.tweets] == ["901"]
    assert len(wire.requests) == 4 and not outcome.degraded and outcome.attempts == 1
