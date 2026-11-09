import pytest

from tests.http_replay import page, tweet

pytestmark = pytest.mark.asyncio(loop_scope="session")


def quoting(tid, quoted):
    obj = tweet(tid, quoted_status_id_str=quoted["rest_id"])
    obj["quoted_status_result"] = {"result": quoted}
    return obj


async def test_search_skips_tweets_that_are_only_quoted(pipeline):
    wire, api = pipeline
    wire.enqueue(page(quoting(921, tweet(920, uid="43"))))

    found = [item async for item in api.search("example")]

    assert [item.id for item in found] == [921]
    assert found[0].quotedTweet is not None and found[0].quotedTweet.id == 920


async def test_search_keeps_a_quoted_tweet_listed_as_its_own_result(pipeline):
    wire, api = pipeline
    quoted = tweet(922, uid="43")
    wire.enqueue(page(quoting(923, quoted), quoted))

    found = [item.id async for item in api.search("example")]

    assert sorted(found) == [922, 923]
