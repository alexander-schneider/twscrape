from pathlib import Path

from twscrape.accounts_pool import AccountsPool


async def test_recreated_db_path_runs_migrations_again(tmp_path: Path):
    db_path = tmp_path / "recreated.db"
    pool = AccountsPool(str(db_path))

    assert await pool.get_all() == []

    db_path.unlink()

    assert await pool.get_all() == []
