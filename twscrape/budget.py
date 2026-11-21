"""Per-account request budget: pacing, daily caps and warm-up (vladkens/twscrape#384).

X suspends fresh accounts that burst and limits accounts that search at a steady high rate. The budget is read
from the environment once, when an AccountsPool is created; everything is off when the variables are unset.

- TWS_ACCOUNT_PACING="SearchTimeline=60,UserTweets=15": minimum seconds between two requests of one account on
  that queue.
- TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=400": requests per account and queue per UTC day.
- TWS_WARMUP_DAYS (default 0 = off) and TWS_WARMUP_START_FRACTION (default 0.1): new accounts start at that
  fraction of the budget and reach the full budget after that many days.
"""

import math
import os
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

PACING_ENV = "TWS_ACCOUNT_PACING"
DAILY_CAPS_ENV = "TWS_ACCOUNT_DAILY_CAPS"
WARMUP_DAYS_ENV = "TWS_WARMUP_DAYS"
WARMUP_START_FRACTION_ENV = "TWS_WARMUP_START_FRACTION"
DEFAULT_WARMUP_START_FRACTION = 0.1
# Queue names are GraphQL operation names; they end up in JSON paths, so nothing else is accepted.
_QUEUE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Reserved keys in an account's stats JSON (objects or strings, so Account.from_rs and the counters skip them).
DAILY_STATS_KEY = "__daily__"  # {"SearchTimeline": {"day": "2026-11-22", "count": 12}}
# The pacing interval is stretched or shortened by a uniform factor, so the rhythm is not machine-regular.
JITTER_LOW, JITTER_HIGH = 0.8, 1.2


def _parse_entries(name: str, raw: str | None, kind: type) -> dict:
    """'Queue=value,Queue=value' into {queue: value}; any malformed entry raises a ValueError naming `name`."""
    result: dict = {}
    if not raw:
        return result
    example = "SearchTimeline=60" if kind is float else "SearchTimeline=400"
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        queue, sep, value = (part.strip() for part in entry.partition("="))
        if not sep or not _QUEUE_NAME.fullmatch(queue):
            raise ValueError(
                f"{name}: invalid entry {entry!r}; expected Queue=value, e.g. {example}"
            )
        if queue in result:
            raise ValueError(f"{name}: queue {queue!r} is given twice")
        try:
            number = int(value) if kind is int else float(value)
        except ValueError:
            raise ValueError(
                f"{name}: invalid value in {entry!r}; expected a positive "
                f"{'whole number' if kind is int else 'number of seconds'}"
            ) from None
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name}: invalid value in {entry!r}; it must be greater than 0")
        result[queue] = number
    return result


def _parse_number(
    name: str, raw: str | None, default: float, low: float, high: float | None
) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        number = float(raw)
    except ValueError:
        raise ValueError(f"{name}: {raw!r} is not a number") from None
    if not math.isfinite(number) or number < low or (high is not None and number > high):
        bounds = f">= {low:g}" if high is None else f"in ({low:g}, {high:g}]"
        raise ValueError(f"{name}: {raw!r} must be {bounds}")
    return number


@dataclass(frozen=True)
class AccountBudget:
    pacing: dict[str, float] = field(default_factory=dict)
    daily_caps: dict[str, int] = field(default_factory=dict)
    warmup_days: float = 0.0
    warmup_start_fraction: float = DEFAULT_WARMUP_START_FRACTION

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AccountBudget":
        env = os.environ if env is None else env
        fraction = _parse_number(
            WARMUP_START_FRACTION_ENV,
            env.get(WARMUP_START_FRACTION_ENV),
            DEFAULT_WARMUP_START_FRACTION,
            0.0,
            1.0,
        )
        if fraction <= 0:
            raise ValueError(f"{WARMUP_START_FRACTION_ENV}: {fraction:g} must be in (0, 1]")
        return cls(
            pacing=_parse_entries(PACING_ENV, env.get(PACING_ENV), float),
            daily_caps=_parse_entries(DAILY_CAPS_ENV, env.get(DAILY_CAPS_ENV), int),
            warmup_days=_parse_number(WARMUP_DAYS_ENV, env.get(WARMUP_DAYS_ENV), 0.0, 0.0, None),
            warmup_start_fraction=fraction,
        )

    def applies_to(self, queue: str) -> bool:
        return queue in self.pacing or queue in self.daily_caps

    def cap_for(self, queue: str, fraction: float = 1.0) -> int | None:
        """Requests per UTC day for `queue`, times the warm-up fraction (floor, at least 1); None when uncapped."""
        cap = self.daily_caps.get(queue)
        if cap is None:
            return None
        return max(1, math.floor(cap * fraction))

    def pace_seconds(self, queue: str, fraction: float = 1.0) -> float | None:
        """How long the account stays off `queue` after a request: the interval, divided by the warm-up
        fraction, times a jitter in 0.8..1.2. None when the queue is not paced."""
        interval = self.pacing.get(queue)
        if interval is None:
            return None
        return interval / fraction * random.uniform(JITTER_LOW, JITTER_HIGH)


def utc_day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def next_utc_midnight(now: datetime) -> int:
    """Unix seconds of the next UTC midnight after `now`: when a daily cap ends."""
    day = now.astimezone(timezone.utc).date() + timedelta(days=1)
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
