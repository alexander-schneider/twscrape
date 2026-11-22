from pathlib import Path

from twscrape.accounts_pool import AccountsPool


async def test_recreated_db_path_runs_migrations_again(tmp_path: Path):
    db_path = tmp_path / "recreated.db"
    pool = AccountsPool(str(db_path))

    assert await pool.get_all() == []

    db_path.unlink()

    assert await pool.get_all() == []


async def test_a_cancellation_during_a_write_leaves_the_database_unlocked(tmp_path, monkeypatch):
    """api.adanos.org#3585 / CI flake of 2026-11-22: a caller's deadline cancelled a write while its connection
    committed. The connection stayed open with the write uncommitted, so every other writer got "database is
    locked" for about a minute (5 s busy timeout, 10 retries). A cancelled operation now still commits and closes."""
    import asyncio

    import aiosqlite

    from twscrape.accounts_pool import AccountsPool
    from twscrape.db import execute

    db_file = str(tmp_path / "accounts.db")
    pool = AccountsPool(db_file)
    await pool.add_account("user1", "p", "e", "ep", cookies="auth_token=t; ct0=c")
    real_commit = aiosqlite.Connection.commit
    committing = asyncio.Event()

    async def slow_commit(self):
        committing.set()
        await asyncio.sleep(0.2)
        return await real_commit(self)

    monkeypatch.setattr(aiosqlite.Connection, "commit", slow_commit)
    writer = asyncio.create_task(execute(db_file, "UPDATE accounts SET last_used = NULL WHERE username = 'user1'"))
    await asyncio.wait_for(committing.wait(), 5)
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)
    monkeypatch.setattr(aiosqlite.Connection, "commit", real_commit)

    await asyncio.wait_for(
        execute(db_file, "UPDATE accounts SET error_msg = 'ok' WHERE username = 'user1'"), 4
    )
    assert (await pool.get("user1")).error_msg == "ok"
