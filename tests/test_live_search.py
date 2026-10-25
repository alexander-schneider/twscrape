import pytest

from scripts.check_adanos_live import run


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_search_and_direct_replies():
    report = await run()
    assert report["status"] == "pass", report
    assert report["counts"]["search_tweets"] > 0
    assert report["counts"]["direct_replies"] > 0
