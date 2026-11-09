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


async def test_suspended_account_rotates_to_active_account(pipeline):
    wire, api = pipeline
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )
    wire.enqueue(error_body("Your account is suspended", code=64), page(tweet(903)))

    assert [item.id async for item in api.search("example")] == [903]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-1"]
    saved = await api.pool.get("offline-0")
    assert saved is not None and not saved.active
    assert saved.error_msg == "(64) Your account is suspended"
    assert (await api.pool.get("offline-1")).active


async def test_rate_warning_keeps_data_and_account_with_remaining_quota(pipeline, monkeypatch):
    wire, api = pipeline
    await api.pool.add_account(
        "offline-1", "_", "_", "_", cookies="auth_token=dummy-offline-1; ct0=dummy-csrf-offline-1"
    )
    first_response_headers(monkeypatch, {"x-rate-limit-remaining": "5"})
    body = page(tweet(904), cursor="next")
    body["errors"] = [{"code": 88, "message": "Rate limit exceeded"}]
    wire.enqueue(body, page(tweet(905)))

    assert [item.id async for item in api.search("example")] == [904, 905]
    assert [request["dummy_account"] for request in wire.requests] == ["offline-0", "offline-0"]
    saved = await api.pool.get("offline-0")
    assert saved is not None and saved.active and saved.locks == {} and saved.error_msg is None
