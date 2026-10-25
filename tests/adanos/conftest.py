import asyncio
import os

import pytest
import pytest_asyncio

from scripts.check_adanos_live import ProbeFailure, candidate_identity, load_consumer
from tests.http_replay import PipelineReplay
from tests.test_live_probe import probe_wire  # noqa: F401


@pytest.fixture(scope="session")
def consumer():
    source = os.environ.get("ADANOS_SOURCE_ROOT")
    if not source:
        pytest.fail("ADANOS_SOURCE_ROOT is required for actual-consumer replay")
    try:
        modules = load_consumer(source)
        identity = candidate_identity()
    except (ProbeFailure, ImportError, OSError) as error:
        pytest.fail(f"Actual-consumer import failed ({type(error).__name__})")
    assert identity["candidate_sha"]
    assert modules.identity["consumer_sha"]
    return modules


@pytest_asyncio.fixture(loop_scope="session")
async def wire(monkeypatch, tmp_path, consumer):
    replay = PipelineReplay(monkeypatch, tmp_path, consumer)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        yield replay
        await replay.real_sleep(0.05)
        assert not replay.responses, "Scripted HTTP responses were not exercised"
        assert not unhandled, "Unhandled asynchronous generator cleanup error"
        assert not [
            r for r in replay.logs.records if r["name"] == "asyncio" and r["level"] == "ERROR"
        ]
    finally:
        replay.close()
        loop.set_exception_handler(previous)
