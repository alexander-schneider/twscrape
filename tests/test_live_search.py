import os

import pytest

from scripts.check_live import run


@pytest.mark.live
async def test_live_public_api():
    if not os.getenv("TWS_LIVE_DB") and not (
        os.getenv("TWS_LIVE_USERNAME") or os.getenv("TWS_LIVE_COOKIES")
    ):
        pytest.skip("Provide TWS_LIVE_USERNAME and TWS_LIVE_COOKIES, or TWS_LIVE_DB")
    report = await run()
    assert report["status"] == "pass", report
