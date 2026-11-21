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

import pytest

from twscrape.accounts_pool import AccountsPool
from twscrape.api import API
from twscrape.budget import AccountBudget

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
