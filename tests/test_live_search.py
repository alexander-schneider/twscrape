import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from twscrape import API, AccountsPool
from twscrape.search_queries import build_stock_cashtag_query
from twscrape.utils import parse_cookies


def _env(name: str) -> str | None:
    return os.getenv(f"TWS_LIVE_{name}") or os.getenv(f"TWS_{name}")


def _default_live_db() -> str:
    return str(Path.home() / ".local" / "share" / "twscrape" / "live-check.db")


def _resolve_live_db() -> str:
    return _env("DB") or _default_live_db()


def _require_live_seed_source() -> tuple[str | None, str | None, str]:
    username = _env("USERNAME")
    cookies = _env("COOKIES")
    db_path = _resolve_live_db()

    if username and cookies:
        return username, cookies, db_path

    if Path(db_path).exists():
        return None, None, db_path

    message = (
        "Missing live X seed source. Provide TWS_LIVE_USERNAME/TWS_USERNAME and "
        "TWS_LIVE_COOKIES/TWS_COOKIES, or pre-seed the shared DB via "
        "`make test-live-seed` or `make test-live-seed-prompt`."
    )
    if os.getenv("TWS_REQUIRE_LIVE") == "1":
        pytest.fail(message)
    pytest.skip(message)


async def _build_live_api(
    tmp_path,
    username: str | None,
    cookies: str | None,
    db_path: str,
    proxy: str | None,
) -> tuple[API, str]:
    if username is None or cookies is None:
        shared_db = Path(db_path)
        writable_db = tmp_path / "live-search.db"
        writable_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shared_db, writable_db)

        pool = AccountsPool(str(writable_db), raise_when_no_account=True)
        accounts = await pool.get_all()
        if not accounts:
            message = f"No accounts available in shared live DB: {db_path}"
            if os.getenv("TWS_REQUIRE_LIVE") == "1":
                pytest.fail(message)
            pytest.skip(message)
        return API(pool, proxy=proxy, raise_when_no_account=True), accounts[0].username

    cookie_map = parse_cookies(cookies)
    missing = [key for key in ("auth_token", "ct0") if key not in cookie_map]
    if missing:
        pytest.fail(f"Live cookie payload is missing required keys: {', '.join(missing)}")

    db_file = tmp_path / "live-search.db"
    pool = AccountsPool(str(db_file), raise_when_no_account=True)
    await pool.add_account(
        username=username,
        password="_",
        email=f"{username}@local.invalid",
        email_password="_",
        cookies=cookies,
        proxy=proxy,
    )
    return API(pool, proxy=proxy, raise_when_no_account=True), username


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_search_returns_recent_results(tmp_path):
    username, cookies, db_path = _require_live_seed_source()
    proxy = _env("PROXY")
    probe_user = _env("PROBE_USER") or "xdevelopers"
    limit = int(_env("LIMIT") or "5")

    query = _env("QUERY")
    if query is None:
        ticker = _env("TICKER") or "NVDA"
        hours_back = int(_env("HOURS_BACK") or "24")
        min_faves = int(_env("MIN_FAVES") or "2")
        since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        query = build_stock_cashtag_query(ticker, since, min_faves=min_faves)

    api, account_username = await _build_live_api(tmp_path, username, cookies, db_path, proxy)

    user = await api.user_by_login(probe_user)
    assert user is not None, f"user_by_login failed for @{probe_user}"

    tweets = []
    async for tweet in api.search(query, limit=limit):
        tweets.append(tweet)

    assert tweets, f'search returned 0 tweets for query "{query}"'
    assert all(tweet.id for tweet in tweets)
    assert all(tweet.user and tweet.user.username for tweet in tweets)
    assert account_username
