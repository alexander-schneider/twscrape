import sqlite3
from pathlib import Path

import pytest

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


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _store_state(path: Path) -> tuple[int, int] | None:
    return (path.stat().st_size, _user_version(path)) if path.exists() else None


async def test_existing_only_neither_creates_nor_migrates_a_store(tmp_path: Path, monkeypatch):
    """With TWS_DB_EXISTING_ONLY=1 the pool opens an existing store of the latest migration only: a missing file,
    an empty file, an older migration or a store without the accounts table raises instead of becoming a fresh
    store without accounts, and not as "database is locked", which the pool would retry."""
    monkeypatch.setenv("TWS_DB_EXISTING_ONLY", "1")
    missing = tmp_path / "missing.db"
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    older = tmp_path / "older.db"
    conn = sqlite3.connect(older)
    conn.execute(
        "CREATE TABLE accounts (username TEXT PRIMARY KEY NOT NULL, password TEXT NOT NULL)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    older_size = older.stat().st_size
    no_table = tmp_path / "no_table.db"
    conn = sqlite3.connect(no_table)
    conn.execute("PRAGMA user_version = 4")
    conn.close()
    no_table_size = no_table.stat().st_size

    for path in (missing, empty, older, no_table):
        with pytest.raises(sqlite3.OperationalError) as raised:
            await AccountsPool(str(path)).get_all()
        assert "database is locked" not in str(raised.value)

    assert _store_state(missing) is None
    assert _store_state(empty) == (0, 0)
    assert _store_state(older) == (older_size, 1)
    assert _store_state(no_table) == (no_table_size, 4)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "empty.db",
        "no_table.db",
        "older.db",
    ]


async def test_existing_only_uses_a_store_of_the_latest_migration(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "accounts.db"
    await AccountsPool(str(db_path)).add_account(
        "user1", "p", "e", "ep", cookies="auth_token=t; ct0=c"
    )

    monkeypatch.setenv("TWS_DB_EXISTING_ONLY", "1")
    pool = AccountsPool(str(db_path))
    await pool.set_active("user1", False)

    assert [(account.username, account.active) for account in await pool.get_all()] == [
        ("user1", False)
    ]
    assert _user_version(db_path) == 4


async def test_existing_only_off_still_creates_the_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TWS_DB_EXISTING_ONLY", "0")
    db_path = tmp_path / "accounts.db"

    assert await AccountsPool(str(db_path)).get_all() == []
    assert _user_version(db_path) == 4
