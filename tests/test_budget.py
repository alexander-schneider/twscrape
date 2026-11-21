"""Per-account pacing, daily caps and warm-up (TWS_ACCOUNT_PACING, TWS_ACCOUNT_DAILY_CAPS, TWS_WARMUP_*).

Ways this can fail, covered below:
- a malformed env string is ignored or half-applied instead of failing at API/pool creation;
- an account sends two requests on a paced queue closer than the interval, or always exactly the interval;
- pacing shortens a longer lock (rate limit, refusal, cooldown);
- the daily count misses requests that failed, is not stored before the request, or does not reset at UTC
  midnight; the cap lock ends before midnight;
- warm-up does not slow new accounts, or never ends;
- a pool held by its caps raises the plain NoAccountError (the worker would alarm), or a pool without capped
  accounts raises BudgetExhaustedError;
- a save (login) or a reloaded pool loses the counts, or reset_locks lifts a cap.
"""

import httpx
import pytest
from pytest_httpx import HTTPXMock

from twscrape.accounts_pool import AccountsPool
from twscrape.api import API
from twscrape.budget import AccountBudget
from twscrape.queue_client import QueueClient, ServiceUnavailableError
from twscrape.utils import utc

BUDGET_ENV = (
    "TWS_ACCOUNT_PACING",
    "TWS_ACCOUNT_DAILY_CAPS",
    "TWS_WARMUP_DAYS",
    "TWS_WARMUP_START_FRACTION",
)


@pytest.fixture(autouse=True)
def clean_budget_env(monkeypatch):
    for name in BUDGET_ENV:
        monkeypatch.delenv(name, raising=False)


def test_unset_config_is_off():
    budget = AccountBudget.from_env({})
    assert budget.pacing == {} and budget.daily_caps == {}
    assert budget.warmup_days == 0 and budget.warmup_start_fraction == 0.1
    assert not budget.applies_to("SearchTimeline")


def test_env_strings_are_parsed():
    budget = AccountBudget.from_env(
        {
            "TWS_ACCOUNT_PACING": " SearchTimeline=60, UserTweets=15.5 ,",
            "TWS_ACCOUNT_DAILY_CAPS": "SearchTimeline=400",
            "TWS_WARMUP_DAYS": "14",
            "TWS_WARMUP_START_FRACTION": "0.25",
        }
    )
    assert budget.pacing == {"SearchTimeline": 60.0, "UserTweets": 15.5}
    assert budget.daily_caps == {"SearchTimeline": 400}
    assert (budget.warmup_days, budget.warmup_start_fraction) == (14.0, 0.25)
    assert budget.applies_to("SearchTimeline") and budget.applies_to("UserTweets")
    assert not budget.applies_to("TweetDetail")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TWS_ACCOUNT_PACING", "SearchTimeline"),
        ("TWS_ACCOUNT_PACING", "SearchTimeline=0"),
        ("TWS_ACCOUNT_PACING", "SearchTimeline=-5"),
        ("TWS_ACCOUNT_PACING", "SearchTimeline=abc"),
        ("TWS_ACCOUNT_PACING", "SearchTimeline=nan"),
        ("TWS_ACCOUNT_PACING", "=60"),
        ("TWS_ACCOUNT_PACING", "Search Timeline=60"),
        ("TWS_ACCOUNT_PACING", "Search'Timeline=60"),
        ("TWS_ACCOUNT_PACING", "SearchTimeline=60,SearchTimeline=30"),
        ("TWS_ACCOUNT_DAILY_CAPS", "SearchTimeline=0"),
        ("TWS_ACCOUNT_DAILY_CAPS", "SearchTimeline=1.5"),
        ("TWS_ACCOUNT_DAILY_CAPS", "SearchTimeline"),
        ("TWS_WARMUP_DAYS", "-1"),
        ("TWS_WARMUP_DAYS", "soon"),
        ("TWS_WARMUP_START_FRACTION", "0"),
        ("TWS_WARMUP_START_FRACTION", "1.5"),
        ("TWS_WARMUP_START_FRACTION", "x"),
    ],
)
def test_invalid_entries_raise_a_clear_error_at_pool_and_api_creation(
    tmp_path, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        AccountsPool(str(tmp_path / "accounts.db"))
    with pytest.raises(ValueError, match=name):
        API(str(tmp_path / "accounts.db"))


def test_pool_reads_the_budget_at_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("TWS_ACCOUNT_PACING", "SearchTimeline=60")
    pool = AccountsPool(str(tmp_path / "accounts.db"))
    monkeypatch.setenv("TWS_ACCOUNT_PACING", "SearchTimeline=5")
    assert pool.budget.pacing == {"SearchTimeline": 60.0}


# --- pacing -------------------------------------------------------------------------------------------------

URL = "https://example.com/api"
SEARCH_PAGE = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"x": 1}}}}}


async def _no_sleep(*args, **kwargs):
    return None


async def make_pool(tmp_path, monkeypatch, accounts=2, **env) -> AccountsPool:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _no_sleep)
    pool = AccountsPool(str(tmp_path / "accounts.db"))
    for i in range(1, accounts + 1):
        await pool.add_account(f"user{i}", "_", "_", "_", cookies=f"auth_token=t{i}; ct0=csrf{i}")
    return pool


async def lock_seconds(pool: AccountsPool, username: str, queue="SearchTimeline") -> float:
    lock = (await pool.get(username)).locks.get(queue)
    return 0.0 if lock is None else (lock - utc.now()).total_seconds()


def test_jitter_stays_within_0_8_and_1_2_and_is_not_constant():
    budget = AccountBudget(pacing={"SearchTimeline": 60.0})
    samples = [budget.pace_seconds("SearchTimeline") for _ in range(2000)]
    assert all(48.0 <= s <= 72.0 for s in samples)
    assert max(samples) - min(samples) > 15  # spread over the range, not one machine-regular value
    assert budget.pace_seconds("TweetDetail") is None


@pytest.mark.parametrize("jitter", [0.8, 1.2])
async def test_a_paced_request_locks_the_account_for_interval_times_jitter(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch, jitter
):
    pool = await make_pool(tmp_path, monkeypatch, TWS_ACCOUNT_PACING="SearchTimeline=60")
    monkeypatch.setattr("twscrape.budget.random.uniform", lambda low, high: jitter)
    client = QueueClient(pool, "SearchTimeline")
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)

    async with client:
        first = await client.get(URL)
        assert client.ctx is None  # released with its pacing lock, not kept for the next page
        second = await client.get(URL)

    assert getattr(first, "__username") == "user1" and getattr(second, "__username") == "user2"
    for username in ("user1", "user2"):
        assert 60 * jitter - 2 <= await lock_seconds(pool, username) <= 60 * jitter


async def test_a_failed_request_is_paced_too_and_pacing_beats_a_shorter_cooldown(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    """Transport errors cool the account for 60 s; the 600 s pacing interval is longer and wins."""
    pool = await make_pool(
        tmp_path, monkeypatch, accounts=1, TWS_ACCOUNT_PACING="SearchTimeline=600"
    )
    monkeypatch.setenv("TWS_RAISE_WHEN_NO_ACCOUNT", "1")
    client = QueueClient(pool, "SearchTimeline")
    for _ in range(3):
        httpx_mock.add_exception(httpx.ReadTimeout("slow"))

    async with client:
        with pytest.raises(Exception):
            await client.get(URL)

    assert 480 - 2 <= await lock_seconds(pool, "user1") <= 720


async def test_a_longer_lock_wins_over_pacing(httpx_mock: HTTPXMock, tmp_path, monkeypatch):
    pool = await make_pool(
        tmp_path, monkeypatch, accounts=1, TWS_ACCOUNT_PACING="SearchTimeline=60"
    )
    monkeypatch.setenv("TWS_RAISE_WHEN_NO_ACCOUNT", "1")
    reset = utc.ts() + 900
    httpx_mock.add_response(
        url=URL,
        status_code=429,
        json={"errors": [{"code": 88, "message": "Rate limit exceeded"}]},
        headers={"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(reset)},
    )
    client = QueueClient(pool, "SearchTimeline")

    async with client:
        with pytest.raises(Exception):
            await client.get(URL)

    assert 900 - 5 <= await lock_seconds(pool, "user1") <= 900


async def test_accounts_given_up_after_a_transient_error_keep_their_pacing_lock(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    """Every account failed (X outage): they are released for the next request as before, but not sooner
    than their pacing interval."""
    pool = await make_pool(tmp_path, monkeypatch, TWS_ACCOUNT_PACING="SearchTimeline=600")
    for _ in range(3):
        httpx_mock.add_response(url=URL, status_code=503, text="down")
    client = QueueClient(pool, "SearchTimeline")

    async with client:
        with pytest.raises(ServiceUnavailableError):
            await client.get(URL)

    for username in ("user1", "user2"):
        assert 480 - 2 <= await lock_seconds(pool, username) <= 720


async def test_without_pacing_the_account_is_kept_for_the_next_request(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    pool = await make_pool(tmp_path, monkeypatch)
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    client = QueueClient(pool, "SearchTimeline")

    async with client:
        first, second = await client.get(URL), await client.get(URL)

    assert getattr(first, "__username") == getattr(second, "__username") == "user1"
    assert await lock_seconds(pool, "user1") == 0.0


async def test_requests_wait_for_a_paced_account_instead_of_giving_up(tmp_path, monkeypatch):
    pool = await make_pool(tmp_path, monkeypatch, TWS_ACCOUNT_PACING="SearchTimeline=600")
    waiting = AccountsPool(pool._db_file, wait_timeout=900, raise_when_no_account=True)
    for username in ("user1", "user2"):
        await pool.lock_until(username, "SearchTimeline", utc.ts() + 600)
    slept = []

    async def pacing_ends(interval):
        slept.append(interval)
        await pool.unlock("user2", "SearchTimeline")

    monkeypatch.setattr("twscrape.accounts_pool.asyncio.sleep", pacing_ends)

    account = await waiting.get_for_queue_or_wait("SearchTimeline")
    assert account is not None and account.username == "user2" and slept
