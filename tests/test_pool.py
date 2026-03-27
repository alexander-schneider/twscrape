from twscrape.accounts_pool import GLOBAL_LOCK_QUEUE, AccountsPool
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
