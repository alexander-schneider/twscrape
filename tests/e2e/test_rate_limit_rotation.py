from datetime import datetime, timezone

import httpx
import pytest

from tests.http_replay import error_body, page, tweet
from twscrape.utils import utc

pytestmark = pytest.mark.asyncio(loop_scope="session")


def first_response_headers(monkeypatch, headers, *, html=False):
    original = httpx.AsyncHTTPTransport.handle_async_request
    first = True

    async def respond(transport, request):
        nonlocal first
        response = await original(transport, request)
        if first:
            first = False
            if html:
                response = httpx.Response(
                    response.status_code,
                    text="<html>Rate limit exceeded</html>",
                    headers={"content-type": "text/html"},
                    request=request,
                )
            response.headers.update(headers)
        return response

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)


@pytest.mark.parametrize(
    "status,body_kind,remaining,reset_offset",
    [
        pytest.param(429, "empty", None, None, id="http-429-empty"),
        pytest.param(429, "html", None, None, id="http-429-html"),
        pytest.param(429, "rate_error", 5, None, id="http-429-is-not-a-ban"),
        pytest.param(200, "rate_error", None, None, id="rate-error-without-headers"),
        pytest.param(200, "rate_error", 0, -1, id="rate-error-expired-reset"),
        pytest.param(200, "rate_error", None, 1800, id="rate-error-future-reset"),
    ],
)
async def test_rate_limit_rotates_search_with_queue_cooldown(
    pipeline, monkeypatch, status, body_kind, remaining, reset_offset
):
    wire, api = pipeline
    now = utc.ts()
    monkeypatch.setattr(utc, "ts", staticmethod(lambda: now))
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )
    headers = {}
    if remaining is not None:
        headers["x-rate-limit-remaining"] = str(remaining)
    if reset_offset is not None:
        headers["x-rate-limit-reset"] = str(now + reset_offset)
    first_response_headers(monkeypatch, headers, html=body_kind == "html")
    body = error_body("Rate limit exceeded", code=88) if body_kind == "rate_error" else {}
    wire.enqueue(body, status=status)
    wire.enqueue(page(tweet(901)))

    assert [item.id async for item in api.search("example")] == [901]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-1"]
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.active and saved.error_msg is None
    expected_reset = now + (reset_offset if reset_offset is not None and reset_offset > 0 else 900)
    assert saved.locks == {"SearchTimeline": datetime.fromtimestamp(expected_reset, timezone.utc)}
    wire.enqueue(page(tweet(902, parent=901)), operation="TweetDetail")
    assert [item.id async for item in api.tweet_replies(901)] == [902]
    assert wire.requests[-1]["dummy_account"] == "offline-0"


@pytest.mark.parametrize(
    "code,message",
    [(64, "Your account is suspended"), (326, "Authorization: Denied by access control")],
)
@pytest.mark.parametrize("exhausted_quota", [False, True])
async def test_suspended_account_rotates_to_active_account(
    pipeline, monkeypatch, code, message, exhausted_quota
):
    wire, api = pipeline
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )
    if exhausted_quota:
        first_response_headers(
            monkeypatch,
            {"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(utc.ts() + 1800)},
        )
    wire.enqueue(error_body(message, code=code), page(tweet(903)))

    assert [item.id async for item in api.search("example")] == [903]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-1"]
    saved = await api.pool.get("offline-0")
    assert saved is not None and not saved.active
    assert saved.error_msg == f"({code}) {message}"
    assert (await api.pool.get("offline-1")).active
    wire.enqueue(page(tweet(906, parent=903)), operation="TweetDetail")
    assert [item.id async for item in api.tweet_replies(903)] == [906]
    assert wire.requests[-1]["dummy_account"] == "offline-1"


@pytest.mark.parametrize(
    "code,message",
    [
        (64, "Your account is suspended"),
        (326, "Authorization: Denied by access control"),
        (32, "Could not authenticate you"),
    ],
)
async def test_account_deactivation_preserves_prior_page_usage(pipeline, code, message):
    wire, api = pipeline
    saved = await api.pool.get("offline-0")
    saved.stats = {"SearchTimeline": 4, "TweetDetail": 2}
    await api.pool.save(saved)
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )
    wire.enqueue(page(tweet(907), cursor="next"), error_body(message, code=code), page(tweet(908)))

    assert [item.id async for item in api.search("example")] == [907, 908]
    assert [request["dummy_account"] for request in wire.requests] == [
        "offline-0",
        "offline-0",
        "offline-1",
    ]
    assert [request["payload"]["variables"].get("cursor") for request in wire.requests] == [
        None,
        "next",
        "next",
    ]
    saved = await api.pool.get("offline-0")
    assert not saved.active and saved.error_msg == f"({code}) {message}"
    assert saved.stats == {"SearchTimeline": 5, "TweetDetail": 2}
    assert (await api.pool.get("offline-1")).stats == {"SearchTimeline": 1}
    assert {item["username"]: item["total_req"] for item in await api.pool.accounts_info()} == {
        "offline-0": 7,
        "offline-1": 1,
    }


async def test_rate_warning_keeps_data_and_account_with_remaining_quota(pipeline, monkeypatch):
    wire, api = pipeline
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )
    first_response_headers(monkeypatch, {"x-rate-limit-remaining": "5"})
    body = {
        **page(tweet(904), cursor="next"),
        "errors": [{"code": 88, "message": "Rate limit exceeded"}],
    }
    wire.enqueue(body, page(tweet(905)))

    assert [item.id async for item in api.search("example")] == [904, 905]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-0"]
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.active and saved.locks == {} and saved.error_msg is None
