from pathlib import Path

pytest_plugins = ["pytester"]


def test_live_marker_uses_real_signer_while_offline_remains_mocked(pytester):
    root = Path(__file__).resolve().parents[1]
    pytester.makeini(f"[pytest]\npythonpath = {root}\nasyncio_mode = auto\nmarkers = live\n")
    pytester.makeconftest("from tests.conftest import mock_xclidgenstore")
    pytester.makepyfile("""
import pytest
from twscrape.account import Account
from twscrape.queue_client import XClIdGenStore
from twscrape.xclid import XClIdGen

async def test_offline():
    signer = await XClIdGenStore.get(None)
    assert signer.calc("GET", "/fixture") == "mocked-clid"

@pytest.mark.live
async def test_live(monkeypatch):
    async def create(**kwargs):
        return XClIdGen([1] * 32, "fixture-animation")
    monkeypatch.setattr(XClIdGen, "create", staticmethod(create))
    account = type("Seed", (), {"username": "fixture", "proxy": None,
        "user_agent": "fixture", "cookies": {}})()
    signer = await XClIdGenStore.get(account, fresh=True)
    assert isinstance(signer, XClIdGen)
    assert signer.calc("GET", "/fixture") != "mocked-clid"
""")
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=2)
