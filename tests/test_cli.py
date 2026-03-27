from types import SimpleNamespace

import pytest

import twscrape.cli as cli
from twscrape.queue_client import ApiFeatureUpdateRequiredError, UnexpectedApiError


class FakePool:
    instances = []

    def __init__(self, db, login_config=None):
        self.db = db
        self.login_config = login_config
        self.deleted = None
        self.relogin_usernames = None
        self.reset_called = False
        self.delete_inactive_called = False
        FakePool.instances.append(self)

    async def accounts_info(self):
        return [{"username": "user1", "active": True}]

    async def stats(self):
        return {
            "total": 3,
            "active": 2,
            "inactive": 1,
            "locked_SearchTimeline": 2,
            "locked_Followers": 0,
        }

    async def load_from_file(self, file_path, line_format):
        self.loaded = (file_path, line_format)

    async def delete_accounts(self, usernames):
        self.deleted = usernames

    async def login_all(self):
        return {"total": 1, "success": 1, "failed": 0}

    async def relogin_failed(self):
        self.relogin_failed_called = True

    async def relogin(self, usernames):
        self.relogin_usernames = usernames

    async def reset_locks(self):
        self.reset_called = True

    async def delete_inactive(self):
        self.delete_inactive_called = True


class FakeApi:
    def __init__(self, pool, debug=False):
        self.pool = pool
        self.debug = debug

    async def user_by_login(self, username):
        return SimpleNamespace(json=lambda: {"username": username})

    async def user_by_login_raw(self, username):
        return SimpleNamespace(json=lambda: {"raw_username": username})

    async def search(self, query, limit=-1):
        for idx in range(limit):
            yield SimpleNamespace(json=lambda idx=idx: {"query": query, "index": idx})


@pytest.fixture(autouse=True)
def reset_fake_pool_instances():
    FakePool.instances = []


@pytest.mark.asyncio
async def test_main_version_prints_versions(monkeypatch, capsys):
    monkeypatch.setattr(cli, "version", lambda _: "9.9.9")
    monkeypatch.setattr(cli.sqlite3, "sqlite_version", "3.40.0")

    async def fake_sqlite_version():
        return "3.45.1"

    monkeypatch.setattr(cli, "get_sqlite_version", fake_sqlite_version)

    args = SimpleNamespace(command="version", debug=False)
    await cli.main(args)

    output = capsys.readouterr().out.strip().splitlines()
    assert output == [
        "twscrape: 9.9.9",
        "SQLite runtime: 3.40.0 (3.45.1)",
    ]


@pytest.mark.asyncio
async def test_main_accounts_uses_pool_handler(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "AccountsPool", FakePool)
    monkeypatch.setattr(
        cli, "print_table", lambda rows, hr_after=False: captured.update(rows=rows)
    )

    args = SimpleNamespace(command="accounts", debug=False, db="accounts.db")
    await cli.main(args)

    assert captured["rows"] == [{"username": "user1", "active": True}]


@pytest.mark.asyncio
async def test_main_stats_formats_locked_queues(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(cli, "AccountsPool", FakePool)
    monkeypatch.setattr(
        cli,
        "print_table",
        lambda rows, hr_after=False: captured.update(rows=rows, hr_after=hr_after),
    )

    args = SimpleNamespace(command="stats", debug=False, db="accounts.db")
    await cli.main(args)

    assert captured == {
        "rows": [{"queue": "locked_SearchTimeline", "locked": 2, "available": 0}],
        "hr_after": True,
    }
    assert capsys.readouterr().out.strip().endswith("Total: 3 - Active: 2 - Inactive: 1")


@pytest.mark.asyncio
async def test_main_add_accounts_prints_next_step(monkeypatch, capsys):
    monkeypatch.setattr(cli, "AccountsPool", FakePool)

    args = SimpleNamespace(
        command="add_accounts",
        debug=False,
        db="accounts.db",
        file_path="accounts.txt",
        line_format="username:password:email:email_password",
    )
    await cli.main(args)

    pool = FakePool.instances[-1]
    assert pool.loaded == ("accounts.txt", "username:password:email:email_password")
    assert "twscrape login_accounts" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_api_command_prints_streamed_results(monkeypatch, capsys):
    monkeypatch.setattr(cli, "AccountsPool", FakePool)
    monkeypatch.setattr(cli, "API", FakeApi)

    args = SimpleNamespace(
        command="search", debug=False, db="accounts.db", raw=False, query="x", limit=2
    )
    await cli.main(args)

    assert capsys.readouterr().out.strip().splitlines() == [
        '{"query": "x", "index": 0}',
        '{"query": "x", "index": 1}',
    ]


@pytest.mark.asyncio
async def test_main_unknown_command_exits(monkeypatch):
    monkeypatch.setattr(cli, "AccountsPool", FakePool)
    monkeypatch.setattr(cli, "API", FakeApi)

    args = SimpleNamespace(command="missing", debug=False, db="accounts.db", raw=False)
    with pytest.raises(SystemExit, match="1"):
        await cli.main(args)


def test_run_exits_on_feature_update_error(monkeypatch):
    class FakeParser:
        def parse_args(self):
            return SimpleNamespace(command="search")

    def fake_run(coro):
        coro.close()
        raise ApiFeatureUpdateRequiredError("update required")

    monkeypatch.setattr(cli, "build_parser", lambda: FakeParser())
    monkeypatch.setattr(cli.asyncio, "run", fake_run)

    with pytest.raises(SystemExit, match="1"):
        cli.run()


def test_run_exits_on_unexpected_api_error(monkeypatch):
    class FakeParser:
        def parse_args(self):
            return SimpleNamespace(command="search")

    def fake_run(coro):
        coro.close()
        raise UnexpectedApiError("HTML edge block (403) for UserByRestId")

    monkeypatch.setattr(cli, "build_parser", lambda: FakeParser())
    monkeypatch.setattr(cli.asyncio, "run", fake_run)

    with pytest.raises(SystemExit, match="1"):
        cli.run()
