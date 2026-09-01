from collections import OrderedDict
from contextlib import aclosing

import httpx
import pytest
from pytest_httpx import HTTPXMock

import twscrape.queue_client as queue_client_module
from twscrape.accounts_pool import GLOBAL_LOCK_QUEUE, AccountsPool
from twscrape.queue_client import (
    ApiFeatureUpdateRequiredError,
    QueueClient,
    ServiceUnavailableError,
    UnexpectedApiError,
    XClIdGenStore,
    is_transient_api_error,
)
from twscrape.utils import utc
from twscrape.xclid import XClIdAccountError, XClIdParseError

DB_FILE = "/tmp/twscrape_test_queue_client.db"
URL = "https://example.com/api"
CF = tuple[AccountsPool, QueueClient]


async def get_locked(pool: AccountsPool) -> set[str]:
    rep = await pool.get_all()
    return set([x.username for x in rep if x.locks.get("SearchTimeline", None) is not None])


async def test_lock_account_when_used(httpx_mock: HTTPXMock, client_fixture):
    pool, client = client_fixture

    locked = await get_locked(pool)
    assert len(locked) == 0

    # should lock account on getting it
    await client.__aenter__()
    locked = await get_locked(pool)
    assert len(locked) == 1
    assert "user1" in locked

    # keep locked on request
    httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)
    assert (await client.get(URL)).json() == {"foo": "bar"}

    locked = await get_locked(pool)
    assert len(locked) == 1
    assert "user1" in locked

    # unlock on exit
    await client.__aexit__(None, None, None)
    locked = await get_locked(pool)
    assert len(locked) == 0


async def test_do_not_switch_account_on_200(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    # get account and lock it
    await client.__aenter__()
    locked1 = await get_locked(pool)
    assert len(locked1) == 1

    # make several requests with status=200
    for x in range(1):
        httpx_mock.add_response(url=URL, json={"foo": x}, status_code=200)
        rep = await client.get(URL)
        assert rep is not None
        assert rep.json() == {"foo": x}

    # account should not be switched
    locked2 = await get_locked(pool)
    assert locked1 == locked2

    # unlock on exit
    await client.__aexit__(None, None, None)
    locked3 = await get_locked(pool)
    assert len(locked3) == 0


async def test_xclid_generation_uses_account_metadata(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    _pool, client = client_fixture
    calls = []

    class LocalClIdGenMock:
        def calc(*args, **kwargs):
            return "mocked-clid"

    async def mock_get(cls, acc, proxy=None, fresh=False):
        calls.append((acc.username, proxy, fresh))
        return LocalClIdGenMock()

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(mock_get))

    async with client:
        assert client.ctx is not None
        httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)

        rep = await client.get(URL)

    assert rep is not None
    assert rep.json() == {"foo": "bar"}
    assert calls == [("user1", None, False)]


async def test_missing_session_cookie_deactivates_and_rotates(
    httpx_mock: HTTPXMock, client_fixture: CF
):
    pool, client = client_fixture
    user1 = await pool.get("user1")
    user1.cookies = {"ct0": "csrf1"}
    await pool.save(user1)

    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)
    rep = await client.get(URL)

    assert rep is not None
    assert getattr(rep, "__username") == "user2"
    assert (await pool.get("user1")).active is False


async def test_xclid_account_error_keeps_account_active_and_rotates(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture

    async def fake_get(cls, acc, proxy=None, fresh=False):
        if acc.username == "user1":
            raise XClIdAccountError("Logged-out X web app")
        return type("Gen", (), {"calc": lambda self, *args: "clid"})()

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(fake_get))
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)

    rep = await client.get(URL)

    assert rep is not None
    assert getattr(rep, "__username") == "user2"
    user1 = await pool.get("user1")
    assert user1.active is True
    assert "SearchTimeline" in user1.locks


async def test_xclid_parse_error_aborts_without_changing_account_state(
    client_fixture: CF, monkeypatch
):
    pool, client = client_fixture

    async def fake_get(cls, acc, proxy=None, fresh=False):
        raise XClIdParseError("Signing script not found")

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(fake_get))

    assert await client.get(URL) is None
    user1 = await pool.get("user1")
    assert user1.active is True
    assert "SearchTimeline" not in user1.locks


async def test_ambiguous_json_403_cools_account_without_deactivation(
    httpx_mock: HTTPXMock, client_fixture: CF
):
    pool, client = client_fixture

    # locked account on enter
    await client.__aenter__()
    locked1 = await get_locked(pool)
    assert len(locked1) == 1

    # fail one request, account should be switched
    httpx_mock.add_response(url=URL, json={"foo": "1"}, status_code=403)
    httpx_mock.add_response(url=URL, json={"foo": "2"}, status_code=200)

    rep = await client.get(URL)
    assert rep is not None
    assert rep.json() == {"foo": "2"}

    locked2 = await get_locked(pool)
    assert len(locked2) == 2
    user1 = await pool.get("user1")
    assert user1.active is True
    assert user1.error_msg is None

    # unlock on exit (failed account still should locked)
    await client.__aexit__(None, None, None)
    locked3 = await get_locked(pool)
    assert len(locked3) == 1
    assert locked1 == locked3  # failed account locked


async def test_explicit_auth_error_deactivates_account(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    httpx_mock.add_response(
        url=URL,
        json={"errors": [{"code": 32, "message": "Could not authenticate you"}]},
        status_code=401,
    )
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)

    rep = await client.get(URL)

    assert rep is not None
    assert getattr(rep, "__username") == "user2"
    user1 = await pool.get("user1")
    assert user1.active is False
    assert user1.error_msg == "(32) Could not authenticate you"


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_retry_with_same_acc_on_network_error(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch, error_type
):
    pool, client = client_fixture
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", fake_sleep)

    # locked account on enter
    await client.__aenter__()
    locked1 = await get_locked(pool)
    assert len(locked1) == 1

    # timeout on first request, account should not be switched
    httpx_mock.add_exception(error_type("Transient transport failure"))
    httpx_mock.add_response(url=URL, json={"foo": "2"}, status_code=200)

    rep = await client.get(URL)
    assert rep is not None
    assert rep.json() == {"foo": "2"}
    assert sleeps == [2]

    locked2 = await get_locked(pool)
    assert locked2 == locked1

    # check username added to request obj (for logging)
    username = getattr(rep, "__username", None)
    assert username is not None


async def test_transport_errors_cool_and_rotate_after_retry_limit(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", fake_sleep)

    async with client:
        for _ in range(3):
            httpx_mock.add_exception(httpx.ReadTimeout("Unable to read within timeout"))
        httpx_mock.add_response(url=URL, json={"ok": True})

        rep = await client.get(URL)

    assert rep is not None
    assert rep.json() == {"ok": True}
    assert getattr(rep, "__username", None) == "user2"
    assert sleeps == [2, 4]

    user1 = await pool.get("user1")
    lock_seconds = (user1.locks["SearchTimeline"] - utc.now()).total_seconds()
    assert 0 < lock_seconds <= 61


async def test_feature_flag_mismatch_raises_typed_error(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    async with client:
        httpx_mock.add_response(
            url=URL,
            json={
                "errors": [
                    {"code": 336, "message": "The following features cannot be null: feature_a"}
                ]
            },
            status_code=400,
        )

        with pytest.raises(ApiFeatureUpdateRequiredError):
            await client.get(URL)

    locked = await get_locked(pool)
    assert len(locked) == 0


async def test_loadshed_retries_with_same_account(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", fake_sleep)

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    httpx_mock.add_response(
        url=URL,
        json={"errors": [{"code": -1, "message": "LoadShed: Unspecified"}]},
        status_code=200,
    )
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)

    rep = await client.get(URL)
    assert rep is not None
    assert rep.json() == {"foo": "ok"}
    assert getattr(rep, "__username", None) == "user1"
    assert sleeps == [2]


async def test_persistent_loadshed_globally_cools_account_and_switches_queue(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", fake_sleep)

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    for _ in range(3):
        httpx_mock.add_response(
            url=URL,
            json={"errors": [{"code": -1, "message": "LoadShed: Unspecified"}]},
            status_code=200,
        )
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)

    rep = await client.get(URL)
    assert rep is not None
    assert rep.json() == {"foo": "ok"}
    assert getattr(rep, "__username", None) == "user2"
    assert sleeps == [2, 4]

    user1 = await pool.get("user1")
    assert "SearchTimeline" in user1.locks
    assert GLOBAL_LOCK_QUEUE in user1.locks

    async with QueueClient(pool, "TweetDetail") as tweet_detail_client:
        assert tweet_detail_client.ctx is not None
        assert tweet_detail_client.ctx.acc.username == "user2"


async def test_api_errors_with_data_are_returned_and_throttled(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    _pool, client = client_fixture
    logs = []
    monkeypatch.setattr(queue_client_module.LogOnce, "pending", OrderedDict())
    monkeypatch.setattr(
        queue_client_module.logger, "log", lambda level, message: logs.append((level, message))
    )

    errors = [
        {"code": -1, "message": "Dependency: Unspecified"},
        {"code": -1, "message": "LoadShed: Unspecified"},
    ]
    for response_errors in (errors, list(reversed(errors))):
        httpx_mock.add_response(
            url=URL,
            json={
                "data": {"search_by_raw_query": {"items": [1]}},
                "errors": response_errors,
            },
        )

        rep = await client.get(URL)
        assert rep is not None
        assert rep.json()["data"]["search_by_raw_query"]["items"] == [1]

    assert len(logs) == 1
    assert logs[0][0] == "DEBUG"
    assert "SearchTimeline" in logs[0][1]
    assert "user1" not in logs[0][1]


async def test_unknown_api_errors_fail_closed_and_cool_account(
    httpx_mock: HTTPXMock, client_fixture: CF
):
    pool, client = client_fixture

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    httpx_mock.add_response(
        url=URL,
        json={"errors": [{"code": 999, "message": "New safety gate required"}]},
        status_code=200,
    )

    with pytest.raises(UnexpectedApiError, match="New safety gate required"):
        await client.get(URL)

    assert client.ctx is None

    user1 = await pool.get("user1")
    assert "SearchTimeline" in user1.locks

    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)
    rep = await client.get(URL)
    assert rep is not None
    assert rep.json() == {"foo": "ok"}
    assert getattr(rep, "__username", None) == "user2"


@pytest.mark.parametrize("data", [None, {"user": {}}])
async def test_malformed_api_errors_fail_closed(httpx_mock: HTTPXMock, client_fixture: CF, data):
    pool, client = client_fixture

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    payload = {"errors": [{"code": 999}]}
    if data is not None:
        payload["data"] = data
    httpx_mock.add_response(url=URL, json=payload, status_code=200)

    with pytest.raises(UnexpectedApiError, match="Malformed API errors payload"):
        await client.get(URL)

    assert client.ctx is None
    user1 = await pool.get("user1")
    assert "SearchTimeline" in user1.locks


async def test_service_unavailable_raises_typed_error_without_cooling_account_after_retries(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    for _ in range(3):
        httpx_mock.add_response(
            url=URL,
            json={"errors": [{"code": -1, "message": "ServiceUnavailable: Unspecified"}]},
            status_code=200,
        )

    with pytest.raises(ServiceUnavailableError, match="ServiceUnavailable"):
        await client.get(URL)

    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    user1 = await pool.get("user1")
    assert "SearchTimeline" in user1.locks


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("(-1) ServiceUnavailable: Unspecified", True),
        ("(-1) Internal server error", True),
        ("(-1) Dependency: Unspecified", True),
        ("(-1) DeadlineExceeded: Unspecified", True),
        ("(29) Timeout: Unspecified", True),
        ("(999) New safety gate required; (-1) Internal server error", True),
        ("(-1) Internal server error; (999) New safety gate required", True),
        ("(32) Could not authenticate you", False),
        ("(64) Your account is suspended", False),
        ("(88) Rate limit exceeded", False),
        ("(999) New safety gate required", False),
    ],
)
def test_transient_api_error_detection(message: str, expected: bool):
    assert is_transient_api_error(message) is expected


async def _mock_sleep(*args, **kwargs):
    return None


async def test_transient_x_api_error_retries_with_same_account(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    httpx_mock.add_response(
        url=URL,
        json={"errors": [{"code": -1, "message": "Internal server error"}]},
        status_code=200,
    )
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)

    rep = await client.get(URL)

    assert rep is not None
    assert rep.json() == {"foo": "ok"}
    assert getattr(rep, "__username", None) == "user1"

    locked = await get_locked(pool)
    assert locked == {"user1"}


@pytest.mark.parametrize(
    "error",
    [
        {"code": -1, "message": "Internal server error"},
        {"code": -1, "message": "Dependency: Unspecified"},
        {"code": -1, "message": "DeadlineExceeded: Unspecified"},
        {"code": 29, "message": "Timeout: Unspecified"},
    ],
)
async def test_transient_x_api_errors_raise_typed_error_without_cooling_account_after_retries(
    error: dict[str, int | str],
    httpx_mock: HTTPXMock,
    client_fixture: CF,
    monkeypatch,
):
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    for _ in range(3):
        httpx_mock.add_response(
            url=URL,
            json={"errors": [error]},
            status_code=200,
        )

    with pytest.raises(ServiceUnavailableError, match=str(error["message"])) as exc_info:
        await client.get(URL)
    assert str(exc_info.value).startswith("Transient X API error")

    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    user1 = await pool.get("user1")
    assert "SearchTimeline" in user1.locks


async def test_html_403_cools_only_the_current_queue(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    httpx_mock.add_response(
        url=URL,
        text="<html><head><title>Attention Required! | Cloudflare</title></head></html>",
        status_code=403,
        headers={"content-type": "text/html; charset=UTF-8"},
    )

    with pytest.raises(UnexpectedApiError, match="HTML edge block"):
        await client.get(URL)

    assert client.ctx is None

    user1 = await pool.get("user1")
    assert user1.active is True
    assert "SearchTimeline" in user1.locks

    async with QueueClient(pool, "TweetDetail") as tweet_detail_client:
        assert tweet_detail_client.ctx is not None
        assert tweet_detail_client.ctx.acc.username == "user1"


async def test_ctx_closed_on_break(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    async def get_data_stream():
        async with client as c:
            counter = 0
            while True:
                counter += 1
                check_retry = counter == 2
                before_ctx = c.ctx

                if check_retry:
                    httpx_mock.add_response(url=URL, json={"counter": counter}, status_code=403)
                    httpx_mock.add_response(url=URL, json={"counter": counter}, status_code=200)
                else:
                    httpx_mock.add_response(url=URL, json={"counter": counter}, status_code=200)

                rep = await c.get(URL)

                if check_retry:
                    assert before_ctx != c.ctx
                elif before_ctx is not None:
                    assert before_ctx == c.ctx

                assert rep is not None
                assert rep.json() == {"counter": counter}
                yield rep.json()["counter"]

                if counter == 9:
                    return

    # need to use async with to break to work
    async with aclosing(get_data_stream()) as gen:
        async for x in gen:
            if x == 3:
                break

    # ctx should be None after break
    assert client.ctx is None
