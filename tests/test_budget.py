"""Per-account pacing, daily caps and warm-up (TWS_ACCOUNT_PACING, TWS_ACCOUNT_DAILY_CAPS, TWS_WARMUP_*).

Ways this can fail, covered below:
- a malformed env string is ignored or half-applied instead of failing at API/pool creation;
- an account sends two requests on a paced queue closer than the interval, or always exactly the interval;
- pacing shortens a longer lock (rate limit, refusal, cooldown);
- the daily count misses requests that failed, is not stored before the request, or does not reset at UTC
  midnight; the cap lock ends before midnight;
- warm-up does not slow new accounts, or never ends;
- a pool held by its caps, wholly or in part (the rest held otherwise), raises the plain NoAccountError (the
  worker would take it for an outage and start its paid fallback), or a pool without capped accounts raises
  BudgetExhaustedError;
- a save (login) or a reloaded pool loses the counts, or reset_locks lifts a cap.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pytest_httpx import HTTPXMock

import twscrape
from twscrape.accounts_pool import (
    GLOBAL_LOCK_QUEUE,
    AccountsPool,
    BudgetExhaustedError,
    NoAccountError,
)
from twscrape.api import API
from twscrape.budget import AccountBudget
from twscrape.db import execute, fetchone
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
CONTROL_URL = re.compile(r"https://x\.com/i/api/graphql/[^/]+/UserByScreenName\?.*")
CONTROL_OK = {"data": {"user": {"result": {"__typename": "User", "rest_id": "783214"}}}}
SEARCH_PAGE = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"x": 1}}}}}


async def _no_sleep(*args, **kwargs):
    return None


async def make_pool(tmp_path, monkeypatch, accounts=2, **env) -> AccountsPool:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _no_sleep)
    monkeypatch.setenv("TWS_RAISE_WHEN_NO_ACCOUNT", "1")  # fail instead of waiting for an account
    pool = AccountsPool(str(tmp_path / "accounts.db"))
    for i in range(1, accounts + 1):
        await pool.add_account(f"user{i}", "_", "_", "_", cookies=f"auth_token=t{i}; ct0=csrf{i}")
    return pool


async def lock_seconds(pool: AccountsPool, username: str, queue="SearchTimeline") -> float:
    lock = (await pool.get(username)).locks.get(queue)
    return 0.0 if lock is None else (lock - utc.now()).total_seconds()


def test_jitter_only_stretches_the_interval_and_is_not_constant():
    """Review round 1 (P1): a 0.8..1.2 jitter let 60 s become 48 s. The interval is a minimum: 1.0..1.4."""
    budget = AccountBudget(pacing={"SearchTimeline": 60.0})
    samples = [budget.pace_seconds("SearchTimeline") or 0.0 for _ in range(2000)]
    assert all(60.0 <= s <= 84.0 for s in samples)
    assert max(samples) - min(samples) > 15  # spread over the range, not one machine-regular value
    assert budget.pace_seconds("TweetDetail") is None


@pytest.mark.parametrize("jitter", [1.0, 1.4])
async def test_a_paced_request_locks_the_account_for_interval_times_jitter(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch, jitter
):
    pool = await make_pool(tmp_path, monkeypatch, TWS_ACCOUNT_PACING="SearchTimeline=60")
    bounds = []

    def fixed_jitter(low, high):
        bounds.append((low, high))
        return jitter

    monkeypatch.setattr("twscrape.budget.random.uniform", fixed_jitter)
    client = QueueClient(pool, "SearchTimeline")
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)

    async with client:
        first = await client.get(URL)
        assert client.ctx is None  # released with its pacing lock, not kept for the next page
        second = await client.get(URL)

    assert getattr(first, "__username") == "user1" and getattr(second, "__username") == "user2"
    for username in ("user1", "user2"):
        assert 60 * jitter - 2 <= await lock_seconds(pool, username) <= 60 * jitter + 1
    assert set(bounds) == {(1.0, 1.4)}


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

    assert 600 - 2 <= await lock_seconds(pool, "user1") <= 840 + 1


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
        assert 600 - 2 <= await lock_seconds(pool, username) <= 840 + 1


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


# --- daily caps ---------------------------------------------------------------------------------------------


async def raw_stats(pool: AccountsPool, username: str) -> dict:
    rs = await fetchone(
        pool._db_file, "SELECT stats FROM accounts WHERE username = :u", {"u": username}
    )
    assert rs is not None
    return json.loads(rs[0])


def next_utc_midnight(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)


def requested_by(httpx_mock: HTTPXMock) -> list[str]:
    return [r.headers["x-csrf-token"].removeprefix("csrf") for r in httpx_mock.get_requests()]


async def test_the_cap_locks_the_account_until_the_next_utc_midnight(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    pool = await make_pool(tmp_path, monkeypatch, TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=2")
    for _ in range(3):
        httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    client = QueueClient(pool, "SearchTimeline")

    async with client:
        users = [getattr(await client.get(URL), "__username") for _ in range(3)]

    assert users == ["user1", "user1", "user2"]
    today = utc.now().date().isoformat()
    assert (await raw_stats(pool, "user1"))["__daily__"] == {
        "SearchTimeline": {"day": today, "count": 2}
    }
    assert (await pool.get("user1")).locks["SearchTimeline"] == next_utc_midnight(utc.now())
    assert (await raw_stats(pool, "user2"))["__daily__"]["SearchTimeline"]["count"] == 1
    assert "SearchTimeline" not in (await pool.get("user2")).locks


async def test_every_request_sent_is_counted_before_it_is_sent(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    """X counts requests, not successes: a failed attempt counts, and the count is stored before sending."""
    pool = await make_pool(
        tmp_path, monkeypatch, accounts=1, TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=10"
    )
    seen_counts = []

    async def record_stored_count(request: httpx.Request) -> httpx.Response:
        stats = await raw_stats(pool, "user1")
        seen_counts.append(stats["__daily__"]["SearchTimeline"]["count"])
        if len(seen_counts) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=SEARCH_PAGE)

    httpx_mock.add_callback(record_stored_count, url=URL, is_reusable=True)
    client = QueueClient(pool, "SearchTimeline")

    async with client:
        await client.get(URL)

    assert seen_counts == [1, 2]
    assert (await raw_stats(pool, "user1"))["__daily__"]["SearchTimeline"]["count"] == 2


async def test_reset_locks_does_not_lift_a_cap_but_the_next_utc_day_does(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    pool = await make_pool(tmp_path, monkeypatch, TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=1")
    for _ in range(3):
        httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    client = QueueClient(pool, "SearchTimeline")

    async with client:
        assert getattr(await client.get(URL), "__username") == "user1"
    await pool.reset_locks()
    # Same day: user1 is still at its cap, so it sends nothing and goes back to its midnight lock.
    async with client:
        assert getattr(await client.get(URL), "__username") == "user2"
    assert requested_by(httpx_mock) == ["1", "2"]
    assert (await pool.get("user1")).locks["SearchTimeline"] == next_utc_midnight(utc.now())
    assert (await raw_stats(pool, "user1"))["__daily__"]["SearchTimeline"]["count"] == 1

    tomorrow = utc.now() + timedelta(days=1)
    monkeypatch.setattr(utc, "now", staticmethod(lambda: tomorrow))
    await pool.reset_locks()
    async with client:
        assert getattr(await client.get(URL), "__username") == "user1"
    assert (await raw_stats(pool, "user1"))["__daily__"]["SearchTimeline"] == {
        "day": tomorrow.date().isoformat(),
        "count": 1,
    }


async def test_uncapped_queues_are_not_counted(httpx_mock: HTTPXMock, tmp_path, monkeypatch):
    pool = await make_pool(
        tmp_path, monkeypatch, accounts=1, TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=5"
    )
    httpx_mock.add_response(url=URL, json={"data": {"x": 1}})
    async with QueueClient(pool, "TweetDetail") as client:
        await client.get(URL)
    assert "__daily__" not in await raw_stats(pool, "user1")


# --- warm-up ------------------------------------------------------------------------------------------------


async def set_stat(pool: AccountsPool, username: str, key: str, value) -> None:
    await execute(
        pool._db_file,
        f"UPDATE accounts SET stats = json_set(stats, '$.\"{key}\"', json(:v)) WHERE username = :u",
        {"u": username, "v": json.dumps(value)},
    )


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [(None, 0.1), (0, 0.1), (5, 0.55), (10, 1.0), (30, 1.0)],
)
def test_the_warm_up_fraction_ramps_from_the_start_fraction_to_one(age_days, expected):
    """Age counts whole days up to the start of the current UTC day."""
    day_start = datetime(2026, 11, 22, tzinfo=timezone.utc)
    first_used = None if age_days is None else day_start - timedelta(days=age_days)
    budget = AccountBudget(warmup_days=10, warmup_start_fraction=0.1)
    for now in (day_start, day_start + timedelta(hours=12), day_start + timedelta(hours=23.9)):
        assert budget.fraction(first_used, now) == pytest.approx(expected)
        assert AccountBudget().fraction(first_used, now) == 1.0  # warm-up off


def test_the_warm_up_fraction_is_stable_within_a_utc_day_and_steps_at_midnight():
    """Review round 1 (P1): a cap that grows during the day turns a capped account into an uncapped one hours
    after its midnight lock was set. The fraction uses the age at the start of the UTC day."""
    budget = AccountBudget(warmup_days=10, warmup_start_fraction=0.1)
    first_used = datetime(
        2026, 11, 17, 12, tzinfo=timezone.utc
    )  # 4.5 days before 2026-11-22 00:00
    morning = budget.fraction(first_used, datetime(2026, 11, 22, 0, 30, tzinfo=timezone.utc))
    evening = budget.fraction(first_used, datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc))
    tomorrow = budget.fraction(first_used, datetime(2026, 11, 23, 0, 30, tzinfo=timezone.utc))
    assert morning == evening == pytest.approx(0.505)
    assert tomorrow == pytest.approx(0.595)
    # Used for the first time today: day 0 all day.
    today = datetime(2026, 11, 22, 18, tzinfo=timezone.utc)
    assert budget.fraction(today, today + timedelta(hours=5)) == pytest.approx(0.1)


def start_of_utc_day(now: datetime) -> datetime:
    return datetime.combine(now.date(), datetime.min.time(), timezone.utc)


async def test_an_account_at_its_warm_up_cap_stays_capped_later_that_day(tmp_path, monkeypatch):
    """Cap reached at 10:00, checked again at 12:00 the same UTC day: still out of capacity, not an outage."""
    pool = await make_pool(
        tmp_path,
        monkeypatch,
        accounts=1,
        TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=400",
        TWS_WARMUP_DAYS="10",
    )
    ten = start_of_utc_day(utc.now()) + timedelta(hours=10)
    await set_stat(pool, "user1", "__first_used__", (ten - timedelta(days=1)).isoformat())
    # Day 1 of 10 counted from the start of today: 14 h old -> fraction 0.1525 -> cap 61.
    for now in (ten, ten + timedelta(hours=2)):
        monkeypatch.setattr(utc, "now", staticmethod(lambda now=now: now))
        await hold_by_cap(pool, "user1", 61)
        assert await pool.accounts_held_by_cap("SearchTimeline") == ["user1"]
        with pytest.raises(BudgetExhaustedError):
            await waiting_pool(pool, wait_timeout=900).get_for_queue_or_wait("SearchTimeline")


@pytest.mark.parametrize(
    ("age_days", "interval", "cap"),
    [(None, 600, 1), (5, 60 / 0.55, 5), (20, 60, 10)],
)
async def test_warm_up_slows_pacing_and_lowers_the_cap(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch, age_days, interval, cap
):
    """Day 0 (a new account): 60 s / 0.1 = 600 s and cap 10 x 0.1 = 1. Day 5 of 10: fraction 0.55. Day 20: off."""
    pool = await make_pool(
        tmp_path,
        monkeypatch,
        accounts=1,
        TWS_ACCOUNT_PACING="SearchTimeline=60",
        TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=10",
        TWS_WARMUP_DAYS="10",
    )
    monkeypatch.setattr("twscrape.budget.random.uniform", lambda low, high: 1.0)
    paces = []
    pace_seconds = AccountBudget.pace_seconds

    def recorded_pace(self, queue, fraction=1.0):
        paces.append(pace_seconds(self, queue, fraction))
        return paces[-1]

    monkeypatch.setattr(AccountBudget, "pace_seconds", recorded_pace)
    if age_days is not None:
        first_used = (start_of_utc_day(utc.now()) - timedelta(days=age_days)).isoformat()
        await set_stat(pool, "user1", "__first_used__", first_used)
    # One request short of the expected cap, so this request reaches it and locks until midnight.
    today = utc.now().date().isoformat()
    await set_stat(
        pool, "user1", "__daily__", {"SearchTimeline": {"day": today, "count": cap - 1}}
    )
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)

    async with QueueClient(pool, "SearchTimeline") as client:
        await client.get(URL)

    assert (await raw_stats(pool, "user1"))["__daily__"]["SearchTimeline"]["count"] == cap
    assert (await pool.get("user1")).locks["SearchTimeline"] == next_utc_midnight(utc.now())
    assert paces == [pytest.approx(interval)]


async def test_the_first_request_stores_first_used_once(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    """Pacing alone also starts the warm-up clock; later requests do not move it."""
    pool = await make_pool(
        tmp_path,
        monkeypatch,
        accounts=1,
        TWS_ACCOUNT_PACING="SearchTimeline=1",
        TWS_WARMUP_DAYS="7",
    )
    for _ in range(2):
        httpx_mock.add_response(url=URL, json=SEARCH_PAGE)

    async with QueueClient(pool, "SearchTimeline") as client:
        await client.get(URL)
    first = (await raw_stats(pool, "user1"))["__first_used__"]
    await pool.reset_locks()
    async with QueueClient(pool, "SearchTimeline") as client:
        await client.get(URL)

    assert (await raw_stats(pool, "user1"))["__first_used__"] == first
    assert abs((utc.from_iso(first) - utc.now()).total_seconds()) < 60
    assert (await pool.get("user1")).stats == {"SearchTimeline": 2}


# --- out of budget ------------------------------------------------------------------------------------------


async def hold_by_cap(
    pool: AccountsPool, username: str, count: int, day: str | None = None
) -> None:
    day = day or utc.now().date().isoformat()
    await set_stat(pool, username, "__daily__", {"SearchTimeline": {"day": day, "count": count}})
    midnight = int(next_utc_midnight(utc.now()).timestamp())
    await pool.lock_until(username, "SearchTimeline", midnight)


def waiting_pool(pool: AccountsPool, **kwargs) -> AccountsPool:
    return AccountsPool(pool._db_file, raise_when_no_account=True, **kwargs)


async def test_requests_end_with_budget_exhausted_when_every_account_used_its_cap(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    pool = await make_pool(tmp_path, monkeypatch, TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=1")
    for _ in range(2):
        httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    client = QueueClient(pool, "SearchTimeline")

    async with client:
        assert getattr(await client.get(URL), "__username") == "user1"
        assert getattr(await client.get(URL), "__username") == "user2"
        with pytest.raises(BudgetExhaustedError, match=r"daily cap.*SearchTimeline=1") as exc_info:
            await client.get(URL)

    assert isinstance(exc_info.value, NoAccountError)
    assert BudgetExhaustedError is twscrape.BudgetExhaustedError


@pytest.mark.parametrize("wait_timeout", [None, 900])
async def test_budget_exhausted_when_every_active_account_is_held_by_its_cap(
    tmp_path, monkeypatch, wait_timeout
):
    pool = await make_pool(
        tmp_path, monkeypatch, accounts=3, TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=400"
    )
    for username in ("user1", "user2", "user3"):
        await hold_by_cap(pool, username, 400)
    await pool.set_active("user3", False)  # an inactive account does not count either way
    monkeypatch.setattr("twscrape.accounts_pool.asyncio.sleep", _no_sleep)

    with pytest.raises(BudgetExhaustedError, match=r"2 of 2 active account\(s\)"):
        await waiting_pool(pool, wait_timeout=wait_timeout).get_for_queue_or_wait("SearchTimeline")


@pytest.mark.parametrize("wait_timeout", [None, 900])
@pytest.mark.parametrize("other", ["refused", "cooldown", "global_lock"])
async def test_a_capped_account_among_otherwise_held_ones_is_budget_exhausted(
    tmp_path, monkeypatch, wait_timeout, other
):
    """5 accounts: X refuses 4, the 5th reached its cap. X does not refuse every account, so that is missing
    capacity, not an outage: the app must not start a paid fallback for it (owner rule of api.adanos.org#3565:
    paid search only when X blocks every active account; a daily cap never starts it)."""
    pool = await make_pool(
        tmp_path, monkeypatch, accounts=5, TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=400"
    )
    await hold_by_cap(pool, "user5", 400)
    for i in range(1, 5):
        if other == "refused":
            await pool.lock_until(f"user{i}", "SearchTimeline", utc.ts() + 6 * 3600)
        elif other == "cooldown":
            await pool.lock_until(f"user{i}", "SearchTimeline", utc.ts() + 30 * 60)
        else:
            await pool.lock_until(f"user{i}", GLOBAL_LOCK_QUEUE, utc.ts() + 3 * 3600)
    monkeypatch.setattr("twscrape.accounts_pool.asyncio.sleep", _no_sleep)

    with pytest.raises(BudgetExhaustedError, match=r"1 of 5 active account\(s\) used their daily cap"):
        await waiting_pool(pool, wait_timeout=wait_timeout).get_for_queue_or_wait("SearchTimeline")


@pytest.mark.parametrize(
    "setup",
    ["refused", "count_from_yesterday", "below_cap", "cap_unset"],
)
async def test_without_a_capped_account_it_stays_no_account_error(tmp_path, monkeypatch, setup):
    caps = "" if setup == "cap_unset" else "SearchTimeline=400"
    pool = await make_pool(tmp_path, monkeypatch, accounts=1, TWS_ACCOUNT_DAILY_CAPS=caps)
    yesterday = (utc.now() - timedelta(days=1)).date().isoformat()
    if setup == "count_from_yesterday":
        await hold_by_cap(pool, "user1", 400, day=yesterday)
    elif setup in ("below_cap", "cap_unset"):
        await hold_by_cap(pool, "user1", 399 if setup == "below_cap" else 400)
    else:
        await pool.lock_until("user1", "SearchTimeline", utc.ts() + 6 * 3600)

    with pytest.raises(NoAccountError) as exc_info:
        await waiting_pool(pool, wait_timeout=900).get_for_queue_or_wait("SearchTimeline")
    assert type(exc_info.value) is NoAccountError


async def test_the_cap_that_holds_an_account_includes_its_warm_up(tmp_path, monkeypatch):
    """Day 0 of a 10-day warm-up: a cap of 400 is 40, so 40 requests today hold the account."""
    pool = await make_pool(
        tmp_path,
        monkeypatch,
        accounts=1,
        TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=400",
        TWS_WARMUP_DAYS="10",
    )
    await set_stat(pool, "user1", "__first_used__", utc.now().isoformat())
    await hold_by_cap(pool, "user1", 40)

    with pytest.raises(BudgetExhaustedError, match="SearchTimeline=400"):
        await waiting_pool(pool, wait_timeout=900).get_for_queue_or_wait("SearchTimeline")


# --- persistence --------------------------------------------------------------------------------------------


async def test_budget_state_survives_save_and_a_reloaded_pool(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    """A login saves the loaded Account; every reserved __...__ stats key must survive it, and a new process
    (a reloaded pool) continues the same count. The Account model and the request counters never see them."""
    pool = await make_pool(
        tmp_path, monkeypatch, accounts=1, TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=3"
    )
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    async with QueueClient(pool, "SearchTimeline") as client:
        await client.get(URL)
    await pool.record_refusal("user1", "SearchTimeline")
    await set_stat(
        pool, "user1", "__future_counter__", 7
    )  # a reserved key that happens to be an int
    before = await raw_stats(pool, "user1")

    account = await pool.get("user1")
    assert account.stats == {"SearchTimeline": 1}
    account.stats["SearchTimeline"] = 5
    await pool.save(account)

    after = await raw_stats(pool, "user1")
    for key in ("__daily__", "__first_used__", "__search_refusals__", "__future_counter__"):
        assert after[key] == before[key], key
    assert after["SearchTimeline"] == 5
    assert {i["username"]: i["total_req"] for i in await pool.accounts_info()} == {"user1": 5}

    reloaded = AccountsPool(pool._db_file)
    async with QueueClient(reloaded, "SearchTimeline") as client:
        await client.get(URL)
    assert (await raw_stats(reloaded, "user1"))["__daily__"]["SearchTimeline"]["count"] == 2


async def test_reset_locks_keeps_daily_counts_and_first_used(tmp_path, monkeypatch):
    pool = await make_pool(tmp_path, monkeypatch, accounts=1)
    await set_stat(pool, "user1", "__first_used__", "2026-11-01T00:00:00+00:00")
    await set_stat(
        pool, "user1", "__daily__", {"SearchTimeline": {"day": "2026-11-22", "count": 9}}
    )
    await pool.record_refusal("user1", "SearchTimeline")
    await pool.lock_until("user1", "SearchTimeline", utc.ts() + 3600)

    await pool.reset_locks()

    stats = await raw_stats(pool, "user1")
    assert stats["__first_used__"] == "2026-11-01T00:00:00+00:00"
    assert stats["__daily__"] == {"SearchTimeline": {"day": "2026-11-22", "count": 9}}
    assert "__search_refusals__" not in stats
    assert (await pool.get("user1")).locks == {}


# --- twscrape accounts --------------------------------------------------------------------------------------


async def test_accounts_info_shows_the_effective_budget_and_todays_count(tmp_path, monkeypatch):
    """Day 0 of a 10-day warm-up: 60 s becomes 600 s and a cap of 400 becomes 40; user2 counted yesterday."""
    pool = await make_pool(
        tmp_path,
        monkeypatch,
        TWS_ACCOUNT_PACING="SearchTimeline=60,UserTweets=15",
        TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=400",
        TWS_WARMUP_DAYS="10",
    )
    await set_stat(pool, "user1", "__first_used__", utc.now().isoformat())
    today = utc.now().date().isoformat()
    await set_stat(pool, "user1", "__daily__", {"SearchTimeline": {"day": today, "count": 12}})
    await set_stat(
        pool, "user2", "__daily__", {"SearchTimeline": {"day": "2000-01-01", "count": 99}}
    )
    await set_stat(pool, "user2", "__first_used__", "2000-01-01T00:00:00+00:00")

    info = {item["username"]: item for item in await pool.accounts_info()}

    assert info["user1"].get("budget") == "SearchTimeline 12/40 every 600s; UserTweets every 150s"
    assert info["user2"].get("budget") == "SearchTimeline 0/400 every 60s; UserTweets every 15s"


async def test_without_a_budget_accounts_info_is_unchanged(tmp_path, monkeypatch):
    pool = await make_pool(tmp_path, monkeypatch, accounts=1)
    (item,) = await pool.accounts_info()
    assert "budget" not in item


# --- pacing before every send -------------------------------------------------------------------------------


class FakeClock:
    """utc.now() and asyncio.sleep on one clock, so a pacing wait shows up as elapsed time."""

    def __init__(self, monkeypatch):
        self.t = utc.now().timestamp()
        monkeypatch.setattr(
            utc, "now", staticmethod(lambda: datetime.fromtimestamp(self.t, timezone.utc))
        )

        async def sleep(seconds, *args, **kwargs):
            self.t += max(0.0, seconds)

        monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", sleep)


async def test_retries_within_one_context_wait_for_the_pacing_interval(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    """Review round 1 (P1): the pacing lock only applied when the context was released, so a transport retry
    went out right after the failed send. Every send of a paced account waits for last send + interval."""
    pool = await make_pool(
        tmp_path, monkeypatch, accounts=1, TWS_ACCOUNT_PACING="SearchTimeline=60"
    )
    clock = FakeClock(monkeypatch)
    sent_at = []

    async def respond(request: httpx.Request) -> httpx.Response:
        sent_at.append(clock.t)
        if len(sent_at) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=SEARCH_PAGE)

    httpx_mock.add_callback(respond, url=URL, is_reusable=True)

    async with QueueClient(pool, "SearchTimeline") as client:
        rep = await client.get(URL)

    assert getattr(rep, "__username") == "user1" and len(sent_at) == 2
    assert sent_at[1] - sent_at[0] >= 60
    # Across contexts: the account is locked until at least its last send plus the interval.
    lock = (await pool.get("user1")).locks["SearchTimeline"]
    assert lock.timestamp() >= sent_at[1] + 60


async def test_a_paced_search_signs_once_and_a_404_still_takes_the_refusal_path(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch
):
    """Three signature attempts on a 404 would take 3 x interval (30 min on warm-up day 0). A paced queue signs
    once; the 404 then goes to the control read and the account is quarantined from search as before."""
    pool = await make_pool(tmp_path, monkeypatch, TWS_ACCOUNT_PACING="SearchTimeline=60")
    httpx_mock.add_response(url=URL, status_code=404, text="")
    httpx_mock.add_response(url=CONTROL_URL, json=CONTROL_OK)
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)

    async with QueueClient(pool, "SearchTimeline") as client:
        rep = await client.get(URL)

    assert getattr(rep, "__username") == "user2"
    sent = [
        (r.url.path.rsplit("/", 1)[-1], r.headers["x-csrf-token"])
        for r in httpx_mock.get_requests()
    ]
    assert sent == [("api", "csrf1"), ("UserByScreenName", "csrf1"), ("api", "csrf2")]
    assert 1800 - 5 <= await lock_seconds(pool, "user1") <= 1800  # first refusal: 30 minutes


# --- concurrent reservations --------------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [None, 10, 3])
async def test_concurrent_reservations_on_one_account_each_get_their_own_result(
    tmp_path, monkeypatch, cap
):
    """Review round 1 (P2): two concurrent reservations stored two counts but returned [None, 1], because the
    second UPDATE overwrote the row's shared _tx marker before the first SELECT read it."""
    pool = await make_pool(tmp_path, monkeypatch, accounts=1)
    # db's module-wide asyncio.Lock binds to the first event loop that waits on it; give this loop its own.
    monkeypatch.setattr("twscrape.db._lock", asyncio.Lock())
    results = await asyncio.gather(
        *(pool.count_request("user1", "SearchTimeline", cap) for _ in range(5))
    )

    stored = (await raw_stats(pool, "user1"))["__daily__"]["SearchTimeline"]["count"]
    granted = sorted(r for r in results if r is not None)
    if cap is None or cap >= 5:
        assert granted == [1, 2, 3, 4, 5] and stored == 5
    else:
        assert granted == [1, 2, 3] and results.count(None) == 2 and stored == 3


@pytest.mark.parametrize("paced", [False, True])
async def test_a_cancellation_while_the_transport_closes_still_ends_the_lease(
    httpx_mock: HTTPXMock, tmp_path, monkeypatch, paced
):
    """api.adanos.org#3565 council round 4: a caller's deadline cancelled the task while the account's HTTP client
    closed. The context was already cleared, so the in-use lease (15 minutes) stayed instead of the pace floor or
    the release. Unpaced, the context closes on exit; paced, after each send."""
    env = {"TWS_ACCOUNT_PACING": "SearchTimeline=60"} if paced else {}
    pool = await make_pool(tmp_path, monkeypatch, accounts=1, **env)
    httpx_mock.add_response(url=URL, json=SEARCH_PAGE)
    closing = asyncio.Event()

    async def hanging_close(self):
        closing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("twscrape.queue_client.Ctx.aclose", hanging_close)
    client = QueueClient(pool, "SearchTimeline")

    async def run():
        async with client:
            await client.get(URL)

    task = asyncio.create_task(run())
    await asyncio.wait_for(closing.wait(), timeout=5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # let a shielded finalization finish

    seconds = await lock_seconds(pool, "user1")
    if paced:
        assert 58 <= seconds <= 85  # the pace floor, not the in-use lease
    else:
        assert seconds <= 1  # released
