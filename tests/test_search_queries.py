import re
from datetime import datetime, timezone

import pytest

from twscrape.search_queries import build_stock_cashtag_query


def test_build_stock_cashtag_query_matches_reddit_sentiment_shape():
    since = datetime(2026, 1, 26, tzinfo=timezone.utc)

    query = build_stock_cashtag_query("NVDA", since, min_faves=2)

    assert "$NVDA" in query
    assert "min_faves:2" in query
    assert "lang:en" in query
    assert "since:2026-01-26" in query
    assert "-filter:links" in query
    assert "min_replies" not in query


def test_build_stock_cashtag_query_has_until_date():
    since = datetime(2026, 1, 26, tzinfo=timezone.utc)

    query = build_stock_cashtag_query("TSLA", since)

    assert re.search(r"until:\d{4}-\d{2}-\d{2}", query)


def test_build_stock_cashtag_query_normalizes_ticker():
    since = datetime(2026, 1, 26, tzinfo=timezone.utc)

    query = build_stock_cashtag_query("$amd", since)

    assert query.startswith("$AMD ")


def test_build_stock_cashtag_query_rejects_empty_ticker():
    since = datetime(2026, 1, 26, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="ticker must not be empty"):
        build_stock_cashtag_query("   ", since)
