from types import SimpleNamespace
from typing import cast

import pytest
from httpx import Response

from twscrape.account import Account
from twscrape.accounts_pool import AccountsPool
from twscrape.login import (
    LoginConfig,
    LoginProtocolError,
    TaskCtx,
    UnsupportedLoginSubtaskError,
    login,
    next_login_task,
)


class FakeResponse:
    def __init__(self, payload: dict, text: str = ""):
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self):
        self.headers = {}
        self.cookies = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


class FakeImap:
    def __init__(self):
        self.close_called = False
        self.logout_called = False

    def close(self):
        self.close_called = True

    def logout(self):
        self.logout_called = True


def make_account() -> Account:
    return Account(
        username="user1",
        password="pass1",
        email="user1@example.com",
        email_password="mailpass",
        user_agent="ua",
        active=False,
    )


@pytest.mark.asyncio
async def test_next_login_task_raises_on_unknown_subtask():
    acc = make_account()
    ctx = cast(
        TaskCtx,
        SimpleNamespace(
            client=SimpleNamespace(headers={}, cookies={}),
            acc=acc,
            cfg=LoginConfig(),
            prev=None,
            imap=None,
        ),
    )

    with pytest.raises(UnsupportedLoginSubtaskError, match="TotallyNewSubtask"):
        await next_login_task(
            ctx,
            cast(
                Response,
                FakeResponse(
                    {"flow_token": "token", "subtasks": [{"subtask_id": "TotallyNewSubtask"}]}
                ),
            ),
        )

    assert acc.error_msg == "unsupported_login_subtasks=TotallyNewSubtask"


@pytest.mark.asyncio
async def test_next_login_task_raises_when_flow_token_missing():
    acc = make_account()
    ctx = cast(
        TaskCtx,
        SimpleNamespace(
            client=SimpleNamespace(headers={}, cookies={}),
            acc=acc,
            cfg=LoginConfig(),
            prev=None,
            imap=None,
        ),
    )

    with pytest.raises(LoginProtocolError, match="flow_token"):
        await next_login_task(
            ctx,
            cast(Response, FakeResponse({"subtasks": []}, text="missing flow token")),
        )


@pytest.mark.asyncio
async def test_next_login_task_returns_none_on_empty_subtasks():
    acc = make_account()
    ctx = cast(
        TaskCtx,
        SimpleNamespace(
            client=SimpleNamespace(headers={}, cookies={}),
            acc=acc,
            cfg=LoginConfig(),
            prev=None,
            imap=None,
        ),
    )

    result = await next_login_task(
        ctx,
        cast(Response, FakeResponse({"flow_token": "token", "subtasks": []})),
    )

    assert result is None
    assert acc.error_msg is None


@pytest.mark.asyncio
async def test_accounts_pool_login_persists_generic_error_message(
    pool_mock: AccountsPool, monkeypatch
):
    account = make_account()

    async def fake_login(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("twscrape.accounts_pool.login", fake_login)

    assert await pool_mock.login(account) is False
    assert account.error_msg == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_login_closes_prefetched_imap_on_failure(monkeypatch):
    account = make_account()
    fake_imap = FakeImap()
    fake_client = FakeClient()

    monkeypatch.setattr(account, "make_client", lambda proxy=None: fake_client)

    async def fake_imap_login(email, password):
        return fake_imap

    monkeypatch.setattr("twscrape.login.imap_login", fake_imap_login)

    async def fake_guest_token(client):
        return "guest-token"

    async def fake_login_initiate(client):
        return FakeResponse({"subtasks": []}, text="missing flow token")

    monkeypatch.setattr("twscrape.login.get_guest_token", fake_guest_token)
    monkeypatch.setattr("twscrape.login.login_initiate", fake_login_initiate)

    with pytest.raises(LoginProtocolError, match="flow_token"):
        await login(account, cfg=LoginConfig(email_first=True, manual=False))

    assert fake_imap.close_called is True
    assert fake_imap.logout_called is True
