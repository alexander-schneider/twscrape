import asyncio
import socket

import pytest
import pytest_asyncio

from tests.http_replay import PipelineReplay
from twscrape import API


@pytest_asyncio.fixture(loop_scope="session")
async def pipeline(monkeypatch, tmp_path):
    def block_network(*args, **kwargs):
        pytest.fail("External network is disabled in pipeline tests")

    monkeypatch.setattr(socket, "getaddrinfo", block_network)
    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setattr(socket.socket, "connect_ex", block_network)
    replay = PipelineReplay(monkeypatch, tmp_path)
    api = API(str(tmp_path / "accounts.db"), raise_when_no_account=True)
    await api.pool.add_account(
        "offline-0",
        "_",
        "_",
        "_",
        user_agent="offline-verification/1.0",
        cookies="auth_token=dummy-offline-0; ct0=dummy-csrf-offline-0",
    )
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        yield replay, api
        await replay.real_sleep(0.05)
        replay.close()
        assert not unhandled, "Unhandled asynchronous generator cleanup error"
    finally:
        loop.set_exception_handler(previous)
