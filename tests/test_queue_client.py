import re
from collections import OrderedDict
from contextlib import aclosing

import httpx
import pytest
from pytest_httpx import HTTPXMock

import twscrape.queue_client as queue_client_module
from twscrape.accounts_pool import GLOBAL_LOCK_QUEUE, AccountsPool
from twscrape.queue_client import (
    AbortReqError,
    ApiFeatureUpdateRequiredError,
    QueueClient,
    SearchRefusedError,
    ServiceUnavailableError,
    UnexpectedApiError,
    XClIdGenStore,
    is_transient_api_error,
)
from twscrape.utils import utc
from twscrape.xclid import XClIdAccountError, XClIdParseError

DB_FILE = "/tmp/twscrape_test_queue_client.db"
URL = "https://example.com/api"
CONTROL_URL = re.compile(r"https://x\.com/i/api/graphql/[^/]+/UserByScreenName\?.*")
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

    async def mock_get(cls, acc, proxy=None, fresh=False, impersonate=None):
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

    async def fake_get(cls, acc, proxy=None, fresh=False, impersonate=None):
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

    async def fake_get(cls, acc, proxy=None, fresh=False, impersonate=None):
        raise XClIdParseError("Signing script not found")

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(fake_get))

    with pytest.raises(XClIdParseError, match="Signing script not found"):
        await client.get(URL)
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
@pytest.mark.parametrize(
    "errors",
    [
        [{"code": 999}],
        [{"code": -1, "message": "LoadShed: Unspecified"}, {"code": 999}],
    ],
)
async def test_malformed_api_errors_fail_closed(
    httpx_mock: HTTPXMock, client_fixture: CF, data, errors
):
    pool, client = client_fixture

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    payload = {"errors": errors}
    if data is not None:
        payload["data"] = data
    httpx_mock.add_response(url=URL, json=payload, status_code=200)

    with pytest.raises(UnexpectedApiError, match="Malformed X API errors payload"):
        await client.get(URL)

    assert client.ctx is None
    user1 = await pool.get("user1")
    assert "SearchTimeline" in user1.locks


async def test_service_unavailable_raises_typed_error_after_moving_to_free_account(
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

    assert [request.headers["x-csrf-token"] for request in httpx_mock.get_requests()] == [
        "csrf1",
        "csrf2",
        "csrf2",
    ]
    assert client.ctx is not None
    assert client.ctx.acc.username == "user2"
    assert await get_locked(pool) == {"user2"}


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


async def test_transient_x_api_error_moves_to_free_account(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)

    await client.__aenter__()
    assert client.ctx is not None
    assert client.ctx.acc.username == "user1"

    httpx_mock.add_response(
        url=URL,
        json={"errors": [{"code": -1, "message": "ServiceUnavailable: Unspecified"}]},
        status_code=200,
    )
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)

    rep = await client.get(URL)

    assert rep is not None
    assert rep.json() == {"foo": "ok"}
    assert getattr(rep, "__username", None) == "user2"

    locked = await get_locked(pool)
    assert locked == {"user1", "user2"}


@pytest.mark.parametrize(
    "error",
    [
        {"code": -1, "message": "Dependency: Unspecified"},
        {"code": -1, "message": "DeadlineExceeded: Unspecified"},
        {"code": 29, "message": "Timeout: Unspecified"},
    ],
)
async def test_transient_x_api_errors_raise_typed_error_after_moving_to_free_account(
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
    assert client.ctx.acc.username == "user2"
    assert await get_locked(pool) == {"user2"}


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


@pytest.mark.parametrize("empty", [None, {}, []])
async def test_deadline_with_empty_data_moves_to_free_account(
    empty, httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    httpx_mock.add_response(
        url=URL,
        json={
            "errors": [{"code": -1, "message": "DeadlineExceeded: Unspecified"}],
            "data": {"user": empty},
        },
    )
    httpx_mock.add_response(url=URL, json={"data": {"user": {"id": "42"}}})

    async with client:
        rep = await client.get(URL)

    assert rep is not None
    assert rep.json() == {"data": {"user": {"id": "42"}}}
    assert getattr(rep, "__username") == "user2"
    assert len(httpx_mock.get_requests()) == 2
    assert (await pool.get("user1")).active is True
    assert (await pool.get("user1")).locks.keys() == {"SearchTimeline"}
    assert (await pool.get("user2")).locks == {}


@pytest.mark.parametrize("data", [0, False, {"items": []}])
async def test_deadline_preserves_nonempty_direct_data(
    data, httpx_mock: HTTPXMock, client_fixture: CF
):
    _pool, client = client_fixture
    payload = {
        "errors": [{"code": -1, "message": "DeadlineExceeded: Unspecified"}],
        "data": {"value": data},
    }
    httpx_mock.add_response(url=URL, json=payload)

    async with client:
        rep = await client.get(URL)

    assert rep is not None
    assert rep.json() == payload
    assert len(httpx_mock.get_requests()) == 1


async def _search_lock_hours(pool: AccountsPool) -> dict[str, float]:
    now = utc.now()
    return {
        acc.username: round((acc.locks["SearchTimeline"] - now).total_seconds() / 3600, 1)
        for acc in await pool.get_all()
        if acc.locks.get("SearchTimeline") is not None
    }


REFUSED = {
    "data": {"search_by_raw_query": {"search_timeline": {}}},
    "errors": [{"code": -1, "message": "Internal server error"}],
}
# A usable control read: X's own account with its user id.
CONTROL_OK = {"data": {"user": {"result": {"__typename": "User", "rest_id": "783214"}}}}


def _control_requests(httpx_mock: HTTPXMock) -> list[httpx.Request]:
    return [r for r in httpx_mock.get_requests() if r.url.path.endswith("/UserByScreenName")]


async def test_refused_search_quarantines_the_account_and_moves_on(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """X refused search for an account (vladkens/twscrape#367): it stays out of search for 30 minutes on its first
    refusal, not 5, and the request goes on with the next account."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    await client.__aenter__()

    httpx_mock.add_response(url=URL, json=REFUSED, status_code=200)
    httpx_mock.add_response(url=CONTROL_URL, json=CONTROL_OK, status_code=200)
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)

    rep = await client.get(URL)

    assert rep is not None and getattr(rep, "__username", None) == "user2"
    assert [r.headers["x-csrf-token"] for r in _control_requests(httpx_mock)] == ["csrf1"]
    locks = await _search_lock_hours(pool)
    assert locks["user1"] == 0.5
    assert locks["user2"] <= 0.25  # the normal in-use lock


async def test_search_refused_for_every_account_stops_without_retrying_them(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """All accounts refused (2026-11-21): one request per account, then a typed error; the quarantines stay."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    await client.__aenter__()

    for _ in range(2):
        httpx_mock.add_response(url=URL, json=REFUSED, status_code=200)
        httpx_mock.add_response(url=CONTROL_URL, json=CONTROL_OK, status_code=200)

    with pytest.raises(SearchRefusedError, match="Internal server error"):
        await client.get(URL)

    search = [r for r in httpx_mock.get_requests() if r not in _control_requests(httpx_mock)]
    assert [r.headers["x-csrf-token"] for r in search] == ["csrf1", "csrf2"]
    assert await _search_lock_hours(pool) == {"user1": 0.5, "user2": 0.5}
    assert issubclass(SearchRefusedError, ServiceUnavailableError)


async def test_search_404_after_fresh_signing_is_a_refusal(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """A limited account gets HTTP 404 on GET SearchTimeline even with a fresh x-client-transaction-id while a
    signed control read still works; it is quarantined like a refused search instead of failing the request
    (vladkens/twscrape#367, #368)."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    await client.__aenter__()

    for _ in range(3):
        httpx_mock.add_response(url=URL, status_code=404, text="")
    httpx_mock.add_response(url=CONTROL_URL, json=CONTROL_OK, status_code=200)
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)

    rep = await client.get(URL)

    assert rep is not None and getattr(rep, "__username", None) == "user2"
    requests = httpx_mock.get_requests()
    assert [r.headers["x-csrf-token"] for r in requests] == ["csrf1"] * 4 + ["csrf2"]
    assert requests[3].url.path.endswith("/UserByScreenName") and requests[3].method == "GET"
    assert (await _search_lock_hours(pool))["user1"] == 0.5


async def test_search_404_with_a_failing_control_read_is_not_a_search_limit(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """A broken signature or an account suspended everywhere also gets empty 404s. When the signed control read
    fails too, the account cools down for 30 minutes (past the 15-minute wait horizon, so other searches give up
    at once); no 6-hour search quarantine for a signing bug."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    await client.__aenter__()

    for _ in range(3):
        httpx_mock.add_response(url=URL, status_code=404, text="")
    httpx_mock.add_response(url=CONTROL_URL, status_code=404, text="")

    with pytest.raises(AbortReqError):
        await client.get(URL)

    assert [r.headers["x-csrf-token"] for r in httpx_mock.get_requests()] == ["csrf1"] * 4
    assert (await _search_lock_hours(pool))["user1"] == 0.5


async def test_search_refusal_lock_is_configurable(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.setenv("TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS", str(2 * 3600))
    monkeypatch.setenv("TWS_SEARCH_REFUSAL_LOCK_SECONDS", str(5 * 3600))

    assert await _refusal_lock_hours(httpx_mock, pool, client, times=3) == [2.0, 4.0, 5.0]


async def _free_both_accounts(pool: AccountsPool) -> None:
    """Lift the search locks only; reset_locks would also end the refusal streak."""
    for username in ("user1", "user2"):
        await pool.unlock(username, "SearchTimeline")


async def _refuse_user1(httpx_mock: HTTPXMock, pool: AccountsPool, client: QueueClient) -> float:
    """One request whose search user1 refuses (confirmed by the control read); user2 answers. user1's lock."""
    await client._close_ctx()
    await _free_both_accounts(pool)
    httpx_mock.add_response(url=URL, json=REFUSED, status_code=200)
    httpx_mock.add_response(url=CONTROL_URL, json=CONTROL_OK, status_code=200)
    httpx_mock.add_response(url=URL, json={"foo": "ok"}, status_code=200)
    rep = await client.get(URL)
    assert rep is not None and getattr(rep, "__username") == "user2"
    return (await _search_lock_hours(pool))["user1"]


async def _refusal_lock_hours(
    httpx_mock: HTTPXMock, pool: AccountsPool, client: QueueClient, times: int
) -> list[float]:
    return [await _refuse_user1(httpx_mock, pool, client) for _ in range(times)]


async def _search_user1(httpx_mock: HTTPXMock, pool: AccountsPool, client: QueueClient, body):
    await client._close_ctx()
    await _free_both_accounts(pool)
    httpx_mock.add_response(url=URL, json=body, status_code=200)
    rep = await client.get(URL)
    assert rep is not None and getattr(rep, "__username") == "user1"


SEARCH_PAGE = {
    "data": {
        "search_by_raw_query": {
            "search_timeline": {"timeline": {"instructions": [{"type": "TimelineAddEntries"}]}}
        }
    }
}


async def test_consecutive_refusals_double_the_search_lock_up_to_the_cap(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """A short X-wide blip costs each account 30 minutes; an account X keeps refusing reaches 6 hours after a few
    probes: 30 min, 1 h, 2 h, 4 h, 6 h, 6 h."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS", raising=False)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_LOCK_SECONDS", raising=False)

    assert await _refusal_lock_hours(httpx_mock, pool, client, times=6) == [
        0.5,
        1.0,
        2.0,
        4.0,
        6.0,
        6.0,
    ]
    await client._close_ctx()


async def test_a_search_with_timeline_data_resets_the_refusal_escalation(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """The count is per account and persists across requests; only a search with timeline data ends it."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS", raising=False)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_LOCK_SECONDS", raising=False)

    assert await _refusal_lock_hours(httpx_mock, pool, client, times=2) == [0.5, 1.0]
    # A fresh pool object reads the same accounts.db: the count is stored, not kept in memory.
    client.pool = AccountsPool(pool._db_file)
    await _search_user1(httpx_mock, pool, client, {"foo": "no timeline data"})
    assert await _refuse_user1(httpx_mock, pool, client) == 2.0

    await _search_user1(httpx_mock, pool, client, SEARCH_PAGE)
    assert await _refusal_lock_hours(httpx_mock, pool, client, times=2) == [0.5, 1.0]
    await client._close_ctx()

    # The stored count does not leak into the Account model or the per-queue request counters.
    user1 = await pool.get("user1")
    assert user1.stats == {"SearchTimeline": 2}
    assert {i["username"]: i["total_req"] for i in await pool.accounts_info()}["user1"] == 2
    await pool.save(user1)
    assert (await pool.get("user1")).stats == {"SearchTimeline": 2}


async def test_saving_a_loaded_account_keeps_its_refusal_streak(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """A login saves the loaded Account; its refusal streak survives, so the next refusal takes the next step."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS", raising=False)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_LOCK_SECONDS", raising=False)

    assert await pool.record_refusal("user1", "SearchTimeline") == 1
    user1 = await pool.get("user1")
    user1.stats["SearchTimeline"] = 7
    await pool.save(user1)

    assert await _refuse_user1(httpx_mock, pool, client) == 1.0
    await client._close_ctx()
    assert (await pool.get("user1")).stats == {"SearchTimeline": 7}


@pytest.mark.parametrize("via_cli", [False, True])
async def test_reset_locks_also_ends_the_refusal_streak(
    via_cli, httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """An operator who puts an account back with `twscrape reset_locks` gives it a fresh start: its next refusal
    locks for 30 minutes, not the next step."""
    from types import SimpleNamespace

    import twscrape.cli as cli

    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS", raising=False)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_LOCK_SECONDS", raising=False)
    for _ in range(2):
        await pool.record_refusal("user1", "SearchTimeline")

    if via_cli:
        await cli.main(SimpleNamespace(command="reset_locks", debug=False, db=str(pool._db_file)))
    else:
        await pool.reset_locks()

    assert await _refuse_user1(httpx_mock, pool, client) == 0.5
    await client._close_ctx()


async def test_search_503_with_internal_server_error_stays_transient(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """A synthetic 503 that says "Internal server error" is an X outage, not a refused account: it takes the
    transient path of v0.21.3 (move to a free account, release the accounts at the end), with no control read
    and no search quarantine."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.setenv("TWS_RAISE_WHEN_NO_ACCOUNT", "1")  # fail instead of waiting for an account
    await client.__aenter__()
    for _ in range(3):
        httpx_mock.add_response(url=URL, json=REFUSED, status_code=503)

    with pytest.raises(ServiceUnavailableError, match="Internal server error") as exc_info:
        await client.get(URL)

    assert not isinstance(exc_info.value, SearchRefusedError)
    assert _control_requests(httpx_mock) == []
    assert all(hours <= 0.25 for hours in (await _search_lock_hours(pool)).values())


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, {"text": "upstream connect error or disconnect/reset before headers"}),
        (502, {"content": b""}),
        (503, {"json": REFUSED}),
        (500, {"json": {"errors": [{"code": 999, "message": "Something new"}]}}),
        # Not decodable as UTF-8, and a rate-limit header that is not a number (review round 3 of v0.22.2).
        (503, {"content": "Serveur indisponible, réessayez".encode("latin-1")}),
        (503, {"text": "busy", "headers": {"x-rate-limit-remaining": "", "x-rate-limit-reset": "soon"}}),
        # An "errors" field of the wrong shape (review round 4 of v0.22.3).
        (503, {"json": {"errors": 1}}),
        (503, {"json": {"errors": [1, None, {"message": 5}]}}),
        (503, {"json": {"errors": "Internal server error"}}),
    ],
)
async def test_every_search_5xx_is_transient_whatever_the_body(
    status, body, httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """A 5xx is an X outage whatever its body: no 15-minute account lock and no internal retry beyond the
    transient path; the request raises ServiceUnavailableError and leaves every account free."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.setenv("TWS_RAISE_WHEN_NO_ACCOUNT", "1")  # fail instead of waiting for an account
    for _ in range(3):
        httpx_mock.add_response(url=URL, status_code=status, **body)

    async with client:
        with pytest.raises(ServiceUnavailableError, match=f"Transient X API error \\({status}\\)"):
            await client.get(URL)

    assert len(httpx_mock.get_requests()) == 3
    assert _control_requests(httpx_mock) == []
    assert await _search_lock_hours(pool) == {}


CONTROLS_WITHOUT_USER_DATA = [
    {"json": {"data": {"user": {}}}},
    {"json": {"errors": [{"code": -1, "message": "Internal server error"}]}},
    {"json": {"data": {"user": {"result": {"__typename": "UserUnavailable"}}}}},
    {"json": {"data": None}},
    {"json": {"data": {"user": None}}},
    {"json": {"data": {"user": {"result": None}}}},
    {"json": {"data": []}},
    {"json": None},
    {"text": "{not json"},
    # Not a user id: zero, negative or empty (review round 3 of v0.22.2).
    {"json": {"data": {"user": {"result": {"rest_id": "0"}}}}},
    {"json": {"data": {"user": {"result": {"rest_id": 0}}}}},
    {"json": {"data": {"user": {"result": {"rest_id": "-1"}}}}},
    {"json": {"data": {"user": {"result": {"rest_id": "abc"}}}}},
]


@pytest.mark.parametrize("control", CONTROLS_WITHOUT_USER_DATA)
async def test_search_refusal_with_a_control_read_without_user_data_is_not_a_search_limit(
    control, httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """HTTP 200 alone is no proof that the account still reads: a GraphQL error or an empty user is not usable
    data. The refused account then cools down for 30 minutes and the request fails, as for a 404 whose control
    read fails; no search quarantine."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.setenv("TWS_RAISE_WHEN_NO_ACCOUNT", "1")  # fail instead of waiting for an account
    await client.__aenter__()
    httpx_mock.add_response(url=URL, json=REFUSED, status_code=200)
    httpx_mock.add_response(url=CONTROL_URL, status_code=200, **control)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await client.get(URL)

    assert not isinstance(exc_info.value, SearchRefusedError)
    assert [r.headers["x-csrf-token"] for r in httpx_mock.get_requests()] == ["csrf1"] * 2
    assert (await _search_lock_hours(pool))["user1"] == 0.5


@pytest.mark.parametrize("control", CONTROLS_WITHOUT_USER_DATA)
async def test_search_404_with_a_control_read_without_user_data_is_not_a_search_limit(
    control, httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.setenv("TWS_RAISE_WHEN_NO_ACCOUNT", "1")  # fail instead of waiting for an account
    await client.__aenter__()
    for _ in range(3):
        httpx_mock.add_response(url=URL, status_code=404, text="")
    httpx_mock.add_response(url=CONTROL_URL, status_code=200, **control)

    with pytest.raises(AbortReqError) as exc_info:
        await client.get(URL)

    assert not isinstance(exc_info.value, SearchRefusedError)
    assert (await _search_lock_hours(pool))["user1"] == 0.5


async def test_404_on_other_queues_still_aborts(httpx_mock: HTTPXMock, pool_mock: AccountsPool):
    await pool_mock.add_account("user1", "p", "e", "ep", cookies="auth_token=t1; ct0=c1")
    client = QueueClient(pool_mock, "TweetDetail")
    for _ in range(3):
        httpx_mock.add_response(url=URL, status_code=404, text="")

    async with client:
        with pytest.raises(AbortReqError):
            await client.get(URL)


async def test_a_5xx_is_transient_also_in_debug_mode(httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch):
    """Debug mode dumps the response; an undecodable 503 must still be classified first (review round 4)."""
    pool, client = client_fixture
    client.debug = True
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.setenv("TWS_RAISE_WHEN_NO_ACCOUNT", "1")
    for _ in range(3):
        httpx_mock.add_response(url=URL, status_code=503, content="indisponible é".encode("latin-1"))

    async with client:
        with pytest.raises(ServiceUnavailableError, match="Transient X API error \\(503\\)"):
            await client.get(URL)

    assert await _search_lock_hours(pool) == {}


async def test_a_refusal_quarantine_records_its_own_expiry(httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch):
    """api.adanos.org#3585: a caller asks whether accounts are held because X refused them. The refusal streak only
    ends with a search that returns data, so an old streak plus any later lock (a proxy cooldown, an in-use lease)
    looked like a quarantine. The quarantine now records its own expiry, and only that counts."""
    pool, client = client_fixture
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _mock_sleep)
    monkeypatch.delenv("TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS", raising=False)

    await _refuse_user1(httpx_mock, pool, client)
    await client._close_ctx()
    assert await pool.accounts_refused("SearchTimeline") == ["user1"]

    # The quarantine ran out; the streak stays. A new ordinary lock is no refusal.
    await pool.mark_refused_until("user1", "SearchTimeline", utc.ts() - 1)
    await pool.lock_until("user1", "SearchTimeline", utc.ts() + 60)
    assert await pool.accounts_refused("SearchTimeline") == []

    # A search with timeline data ends the streak and the expiry.
    await pool.mark_refused_until("user1", "SearchTimeline", utc.ts() + 600)
    await _search_user1(httpx_mock, pool, client, SEARCH_PAGE)
    await client._close_ctx()
    assert await pool.accounts_refused("SearchTimeline") == []


async def test_reset_locks_ends_the_refusal_expiry(pool_mock: AccountsPool):
    await pool_mock.mark_refused_until("user1", "SearchTimeline", utc.ts() + 600)
    await pool_mock.reset_locks()
    assert await pool_mock.accounts_refused("SearchTimeline") == []


async def test_a_refusal_marker_without_a_live_lock_is_no_refusal(pool_mock: AccountsPool):
    """api.adanos.org#3585 round 2: anything that removes the SearchTimeline lock but keeps the stats (an operator's
    one-account unlock, a relogin, a save of an older Account) left a free account "refused" for hours."""
    await pool_mock.add_account("user1", "p", "e", "ep", cookies="auth_token=t; ct0=c")
    await pool_mock.mark_refused_until("user1", "SearchTimeline", utc.ts() + 600)
    assert await pool_mock.accounts_refused("SearchTimeline") == []
    await pool_mock.lock_until("user1", "SearchTimeline", utc.ts() + 600)
    assert await pool_mock.accounts_refused("SearchTimeline") == ["user1"]
