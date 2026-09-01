import pytest

from twscrape.accounts_pool import GLOBAL_LOCK_QUEUE, AccountsPool
from twscrape.api import API
from twscrape.utils import utc


async def test_add_accounts(pool_mock: AccountsPool):
    # should add account
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    acc = await pool_mock.get("user1")
    assert acc.username == "user1"
    assert acc.password == "pass1"
    assert acc.email == "email1"
    assert acc.email_password == "email_pass1"

    # should not add account with same username
    await pool_mock.add_account("user1", "pass2", "email2", "email_pass2")
    acc = await pool_mock.get("user1")
    assert acc.username == "user1"
    assert acc.password == "pass1"
    assert acc.email == "email1"
    assert acc.email_password == "email_pass1"

    # should not add account with different username case
    await pool_mock.add_account("USER1", "pass2", "email2", "email_pass2")
    acc = await pool_mock.get("user1")
    assert acc.username == "user1"
    assert acc.password == "pass1"
    assert acc.email == "email1"
    assert acc.email_password == "email_pass1"

    # should add account with different username
    await pool_mock.add_account("user2", "pass2", "email2", "email_pass2")
    acc = await pool_mock.get("user2")
    assert acc.username == "user2"
    assert acc.password == "pass2"
    assert acc.email == "email2"
    assert acc.email_password == "email_pass2"


async def test_cookie_account_requires_complete_session(pool_mock: AccountsPool):
    await pool_mock.add_account("missing-auth", "pass", "email", "email_pass", cookies="ct0=csrf")
    await pool_mock.add_account(
        "complete", "pass", "email", "email_pass", cookies="auth_token=token; ct0=csrf"
    )

    assert (await pool_mock.get("missing-auth")).active is False
    assert (await pool_mock.get("complete")).active is True


async def test_add_account_cookies_creates_cookie_account(pool_mock: AccountsPool):
    await pool_mock.add_account_cookies("user1", "auth_token=token; ct0=csrf")

    account = await pool_mock.get("user1")
    assert account.active is True
    assert account.has_session is True
    assert account.login_method == "cookies"


async def test_add_account_cookies_refreshes_existing_account(pool_mock: AccountsPool):
    await pool_mock.add_account(
        "user1",
        "pass",
        "email",
        "email-pass",
        proxy="http://proxy.test",
        cookies="auth_token=old; ct0=old-csrf",
    )
    account = await pool_mock.get("user1")
    account.stats = {"SearchTimeline": 3}
    account.error_msg = "expired"
    await pool_mock.save(account)

    await pool_mock.add_account_cookies("user1", "auth_token=new; ct0=new-csrf")
    refreshed = await pool_mock.get("user1")

    assert refreshed.cookies == {"auth_token": "new", "ct0": "new-csrf"}
    assert refreshed.active is True
    assert refreshed.error_msg is None
    assert refreshed.login_method == "password"
    assert refreshed.password == "pass"
    assert refreshed.proxy == "http://proxy.test"
    assert refreshed.stats == {"SearchTimeline": 3}


async def test_add_account_cookies_requires_full_session(pool_mock: AccountsPool):
    with pytest.raises(ValueError, match="auth_token and ct0"):
        await pool_mock.add_account_cookies("user1", "ct0=csrf")

    assert await pool_mock.get_account("user1") is None


async def test_delete_accounts_handles_special_username(pool_mock: AccountsPool):
    username = 'bad"name'
    await pool_mock.add_account(username, "pass1", "email1", "email_pass1")

    await pool_mock.delete_accounts([username])

    assert await pool_mock.get_account(username) is None


async def test_login_all_handles_special_username(pool_mock: AccountsPool, monkeypatch):
    username = 'bad"name'
    await pool_mock.add_account(username, "pass1", "email1", "email_pass1")

    seen: list[str] = []

    async def fake_login(account):
        seen.append(account.username)
        return True

    monkeypatch.setattr(pool_mock, "login", fake_login)

    stats = await pool_mock.login_all([username])

    assert stats == {"total": 1, "success": 1, "failed": 0}
    assert seen == [username]


async def test_login_all_empty_usernames_returns_empty_stats(pool_mock: AccountsPool):
    stats = await pool_mock.login_all([])

    assert stats == {"total": 0, "success": 0, "failed": 0}


async def test_relogin_handles_special_username(pool_mock: AccountsPool, monkeypatch):
    username = 'bad"name'
    await pool_mock.add_account(username, "pass1", "email1", "email_pass1")
    await pool_mock.set_active(username, True)

    acc = await pool_mock.get(username)
    acc.headers = {"authorization": "Bearer test"}
    acc.cookies = {"ct0": "token"}
    await pool_mock.save(acc)

    relogin_calls: list[list[str]] = []

    async def fake_login_all(usernames):
        relogin_calls.append(usernames)
        return {"total": len(usernames), "success": 0, "failed": 0}

    monkeypatch.setattr(pool_mock, "login_all", fake_login_all)

    await pool_mock.relogin([username])

    acc = await pool_mock.get(username)
    assert acc.active is False
    assert acc.headers == {}
    assert acc.cookies == {}
    assert acc.error_msg is None
    assert relogin_calls == [[username]]


async def test_relogin_skips_cookie_accounts(pool_mock: AccountsPool, monkeypatch):
    await pool_mock.add_account_cookies("user1", "auth_token=token; ct0=csrf")

    async def fail_if_called(account):
        pytest.fail("cookie account entered password login")

    monkeypatch.setattr(pool_mock, "login", fail_if_called)
    await pool_mock.relogin("user1")

    assert (await pool_mock.get("user1")).active is True


async def test_get_all(pool_mock: AccountsPool):
    # should return empty list
    accs = await pool_mock.get_all()
    assert len(accs) == 0

    # should return all accounts
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    await pool_mock.add_account("user2", "pass2", "email2", "email_pass2")
    accs = await pool_mock.get_all()
    assert len(accs) == 2
    assert accs[0].username == "user1"
    assert accs[1].username == "user2"


async def test_save(pool_mock: AccountsPool):
    # should save account
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    acc = await pool_mock.get("user1")
    acc.password = "pass2"
    await pool_mock.save(acc)
    acc = await pool_mock.get("user1")
    assert acc.password == "pass2"

    # should not save account
    acc = await pool_mock.get("user1")
    acc.username = "user2"
    await pool_mock.save(acc)
    acc = await pool_mock.get("user1")
    assert acc.username == "user1"


async def test_get_for_queue(pool_mock: AccountsPool):
    Q = "test_queue"

    # should return account
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    await pool_mock.set_active("user1", True)
    acc = await pool_mock.get_for_queue(Q)
    assert acc is not None
    assert acc.username == "user1"
    assert acc.active is True
    assert acc.locks is not None
    assert Q in acc.locks
    assert acc.locks[Q] is not None

    # should return None
    acc = await pool_mock.get_for_queue(Q)
    assert acc is None


async def test_account_unlock(pool_mock: AccountsPool):
    Q = "test_queue"

    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    await pool_mock.set_active("user1", True)
    acc = await pool_mock.get_for_queue(Q)
    assert acc is not None
    assert acc.locks[Q] is not None

    # should unlock account and make available for queue
    await pool_mock.unlock(acc.username, Q)
    acc = await pool_mock.get_for_queue(Q)
    assert acc is not None
    assert acc.locks[Q] is not None

    # should update lock time
    end_time = utc.ts() + 60  # + 1 minute
    await pool_mock.lock_until(acc.username, Q, end_time)

    acc = await pool_mock.get(acc.username)
    assert int(acc.locks[Q].timestamp()) == end_time


async def test_global_lock_blocks_all_queues(pool_mock: AccountsPool):
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    await pool_mock.set_active("user1", True)

    end_time = utc.ts() + 120
    await pool_mock.lock_until("user1", GLOBAL_LOCK_QUEUE, end_time)

    acc = await pool_mock.get_for_queue("SearchTimeline")
    assert acc is None


async def test_next_available_at_includes_global_locks(pool_mock: AccountsPool):
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    await pool_mock.set_active("user1", True)

    end_time = utc.ts() + 120
    await pool_mock.lock_until("user1", GLOBAL_LOCK_QUEUE, end_time)

    next_available = await pool_mock.next_available_at("SearchTimeline")
    assert next_available is not None


async def test_get_for_queue_or_wait_returns_none_when_timeout_is_zero(pool_mock: AccountsPool):
    queue = "TestQueue"
    pool = AccountsPool(pool_mock._db_file, wait_timeout=0)
    await pool.add_account("user1", "pass1", "email1", "ep1")
    await pool.set_active("user1", True)
    await pool.get_for_queue(queue)

    assert await pool.get_for_queue_or_wait(queue) is None


async def test_get_for_queue_or_wait_polls_until_account_is_available(
    pool_mock: AccountsPool, monkeypatch
):
    queue = "TestQueue"
    pool = AccountsPool(pool_mock._db_file, wait_timeout=1, wait_interval=0.1)
    await pool.add_account("user1", "pass1", "email1", "ep1")
    await pool.set_active("user1", True)
    locked = await pool.get_for_queue(queue)
    assert locked is not None
    intervals = []

    async def fake_sleep(interval):
        intervals.append(interval)
        await pool.unlock("user1", queue)

    monkeypatch.setattr("twscrape.accounts_pool.asyncio.sleep", fake_sleep)

    account = await pool.get_for_queue_or_wait(queue)

    assert account is not None
    assert account.username == "user1"
    assert intervals == [0.1]


async def test_get_for_queue_or_wait_clamps_sleep_to_remaining_timeout(
    pool_mock: AccountsPool, monkeypatch
):
    queue = "TestQueue"
    pool = AccountsPool(pool_mock._db_file, wait_timeout=1, wait_interval=5)
    await pool.add_account("user1", "pass1", "email1", "ep1")
    await pool.set_active("user1", True)
    assert await pool.get_for_queue(queue) is not None
    now = 0.0
    intervals = []

    class FakeLoop:
        def time(self):
            return now

    async def fake_sleep(interval):
        nonlocal now
        intervals.append(interval)
        now += interval

    monkeypatch.setattr("twscrape.accounts_pool.asyncio.get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr("twscrape.accounts_pool.asyncio.sleep", fake_sleep)

    assert await pool.get_for_queue_or_wait(queue) is None
    assert intervals == [1]


def test_api_passes_wait_config_to_new_pool(tmp_path):
    api = API(str(tmp_path / "test.db"), wait_timeout=1, wait_interval=0.1)

    assert api.pool._wait_timeout == 1
    assert api.pool._wait_interval == 0.1


async def test_accounts_info_uses_cookie_session_state(pool_mock: AccountsPool):
    await pool_mock.add_account_cookies("cookie-user", "auth_token=token; ct0=csrf")
    await pool_mock.add_account("password-user", "pass", "email", "email-pass")

    info = {item["username"]: item for item in await pool_mock.accounts_info()}

    assert info["cookie-user"]["logged_in"] is True
    assert info["cookie-user"]["login_method"] == "cookies"
    assert info["password-user"]["logged_in"] is False
    assert info["password-user"]["login_method"] == "password"


async def test_get_stats(pool_mock: AccountsPool):
    Q = "SearchTimeline"

    # should return empty stats
    stats = await pool_mock.stats()
    for k, v in stats.items():
        assert v == 0, f"{k} should be 0"

    # should increate total
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    stats = await pool_mock.stats()
    assert stats["total"] == 1
    assert stats["active"] == 0

    # should increate active
    await pool_mock.set_active("user1", True)
    stats = await pool_mock.stats()
    assert stats["total"] == 1
    assert stats["active"] == 1

    # should update queue stats
    acc = await pool_mock.get_for_queue(Q)
    assert acc is not None
    stats = await pool_mock.stats()
    assert stats["total"] == 1
    assert stats["active"] == 1
    assert stats[f"locked_{Q}"] == 1
