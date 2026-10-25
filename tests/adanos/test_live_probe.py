import os

import pytest

from scripts import check_adanos_live as live
from tests.http_replay import error_body, page
from tests.test_live_probe import ENV, current_tweet

pytestmark = [pytest.mark.adanos, pytest.mark.asyncio(loop_scope="session")]


@pytest.mark.parametrize("reply_fails", [False, True])
async def test_actual_consumer_live_mode_reports_positive_or_swallowed_failure(
    consumer, probe_wire, reply_fails
):
    probe_wire.enqueue(page(current_tweet(901)))
    reply = (
        error_body("Synthetic reply failure", code=999)
        if reply_fails
        else page(current_tweet(902, parent=901))
    )
    probe_wire.enqueue(reply, operation="TweetDetail")
    report = await live.run(ENV | {"ADANOS_SOURCE_ROOT": os.environ["ADANOS_SOURCE_ROOT"]})
    assert report["mode"] == "consumer-live"
    assert report["consumer_sha"] == consumer.identity["consumer_sha"]
    assert report["consumer_imports"] == consumer.identity["consumer_imports"]
    assert report["status"] == ("fail" if reply_fails else "pass")
    assert report["category"] == ("graphql_response" if reply_fails else "accepted")
    assert report["counts"]["direct_replies"] == (0 if reply_fails else 1)
