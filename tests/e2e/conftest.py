import asyncio

import pytest_asyncio

from tests.http_replay import PipelineReplay
from twscrape import API, AccountsPool


@pytest_asyncio.fixture(loop_scope="session")
async def pipeline(monkeypatch, tmp_path):
    replay = PipelineReplay(monkeypatch, tmp_path)
    pool = AccountsPool(str(tmp_path / "accounts.db"), raise_when_no_account=True)
    await pool.add_account(
        "offline-0",
        "_",
        "_",
        "_",
        user_agent="offline-verification/1.0",
        cookies="auth_token=dummy-offline-0; ct0=dummy-csrf-offline-0",
    )
    api = API(pool, proxy="http://offline.invalid:8080")
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        yield replay, api
        await replay.real_sleep(0.05)
        assert not replay.responses, "Scripted HTTP responses were not exercised"
        assert not unhandled, "Unhandled asynchronous generator cleanup error"
    finally:
        replay.close()
        loop.set_exception_handler(previous)
