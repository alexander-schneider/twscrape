import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TypedDict

from fake_useragent import UserAgent
from httpx import HTTPStatusError

from .account import Account
from .budget import DAILY_STATS_KEY, FIRST_USED_STATS_KEY, AccountBudget, utc_day
from .db import execute, fetchall, fetchone
from .logger import logger
from .login import LoginConfig, login
from .utils import get_env_bool, parse_cookies, utc


class NoAccountError(Exception):
    pass


class AccountInfo(TypedDict):
    username: str
    logged_in: bool
    login_method: str
    active: bool
    last_used: datetime | None
    total_req: int
    error_msg: str | None


GLOBAL_LOCK_QUEUE = "__global__"
# Matches the placeholder lock `_get_and_lock` puts on an account in use; such an account comes back when its
# request ends, so only a lock beyond this horizon (plus a minute of slack) means no account is coming.
IN_USE_LOCK_SECONDS = 15 * 60
# Consecutive refusals per queue, stored in the stats JSON as {"__search_refusals__": {"SearchTimeline": 2}}.
# The value is an object, so Account.from_rs (integer counters only) and the request counters skip it.
REFUSALS_STATS_KEY = "__search_refusals__"


def has_required_cookies(cookies: dict[str, str]) -> bool:
    return all(cookies.get(name) for name in ("auth_token", "ct0"))


def guess_delim(line: str):
    lp, rp = tuple([x.strip() for x in line.split("username")])
    return rp[0] if not lp else lp[-1]


class AccountsPool:
    # _order_by: str = "RANDOM()"
    _order_by: str = "username"

    def __init__(
        self,
        db_file="accounts.db",
        login_config: LoginConfig | None = None,
        raise_when_no_account=False,
        wait_timeout: float | None = None,
        wait_interval: float = 5.0,
    ):
        self._db_file = db_file
        self._login_config = login_config or LoginConfig()
        self._raise_when_no_account = raise_when_no_account
        self._wait_timeout = wait_timeout
        self._wait_interval = wait_interval
        # Read once, so a malformed TWS_ACCOUNT_PACING etc. fails here, not on the first request.
        self.budget = AccountBudget.from_env()

    def _usernames_where(self, usernames: list[str]) -> tuple[str, dict[str, str]]:
        placeholders = []
        params: dict[str, str] = {}
        for idx, username in enumerate(dict.fromkeys(usernames)):
            key = f"username_{idx}"
            placeholders.append(f":{key}")
            params[key] = username

        return ",".join(placeholders), params

    async def load_from_file(self, filepath: str, line_format: str):
        line_delim = guess_delim(line_format)
        tokens = line_format.split(line_delim)

        required = set(["username", "password", "email", "email_password"])
        if not required.issubset(tokens):
            raise ValueError(f"Invalid line format: {line_format}")

        accounts = []
        with open(filepath, "r") as f:
            lines = f.read().split("\n")
            lines = [x.strip() for x in lines if x.strip()]

            for line in lines:
                data = [x.strip() for x in line.split(line_delim)]
                if len(data) < len(tokens):
                    raise ValueError(f"Invalid line: {line}")

                data = data[: len(tokens)]
                vals = {k: v for k, v in zip(tokens, data) if k != "_"}
                accounts.append(vals)

        for x in accounts:
            await self.add_account(**x)

    async def add_account(
        self,
        username: str,
        password: str,
        email: str,
        email_password: str,
        user_agent: str | None = None,
        proxy: str | None = None,
        cookies: str | None = None,
        mfa_code: str | None = None,
    ):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if rs:
            logger.warning(f"Account {username} already exists")
            return

        account = Account(
            username=username,
            password=password,
            email=email,
            email_password=email_password,
            user_agent=user_agent or UserAgent().safari,
            active=False,
            locks={},
            stats={},
            headers={},
            cookies=parse_cookies(cookies) if cookies else {},
            proxy=proxy,
            mfa_code=mfa_code,
        )

        if has_required_cookies(account.cookies):
            account.active = True

        await self.save(account)
        logger.info(f"Account {username} added successfully (active={account.active})")

    async def add_account_cookies(self, username: str, cookies: str):
        parsed = parse_cookies(cookies)
        if not has_required_cookies(parsed):
            raise ValueError("Cookies must include auth_token and ct0")

        qs = """
        INSERT INTO accounts (username, password, email, email_password, user_agent, active, cookies)
        VALUES (:username, '_', '_', '_', :user_agent, true, :cookies)
        ON CONFLICT(username) DO UPDATE SET
            cookies = excluded.cookies,
            headers = '{}',
            active = true,
            error_msg = NULL
        """
        params = {
            "username": username,
            "user_agent": UserAgent().safari,
            "cookies": json.dumps(parsed),
        }
        await execute(self._db_file, qs, params)
        logger.info(f"Cookies for account {username} updated successfully")

    async def delete_accounts(self, usernames: str | list[str]):
        usernames = usernames if isinstance(usernames, list) else [usernames]
        usernames = list(set(usernames))
        if not usernames:
            logger.warning("No usernames provided")
            return

        placeholders, params = self._usernames_where(usernames)
        qs = f"DELETE FROM accounts WHERE username IN ({placeholders})"
        await execute(self._db_file, qs, params)

    async def delete_inactive(self):
        qs = "DELETE FROM accounts WHERE active = false"
        await execute(self._db_file, qs)

    async def get(self, username: str):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if not rs:
            raise ValueError(f"Account {username} not found")
        return Account.from_rs(rs)

    async def get_all(self):
        qs = "SELECT * FROM accounts"
        rs = await fetchall(self._db_file, qs)
        return [Account.from_rs(x) for x in rs]

    async def get_account(self, username: str):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if not rs:
            return None
        return Account.from_rs(rs)

    async def save(self, account: Account):
        data = account.to_rs()
        cols = list(data.keys())

        # Account.stats holds only the integer request counters; keep the stored refusal streak, which the
        # loaded Account never carries, instead of erasing it on every save (for example after a login).
        path = f'$."{REFUSALS_STATS_KEY}"'
        keep_streak = f"""stats=CASE
            WHEN json_extract(accounts.stats, '{path}') IS NULL THEN excluded.stats
            ELSE json_set(excluded.stats, '{path}', json(json_extract(accounts.stats, '{path}')))
        END"""
        updates = [keep_streak if x == "stats" else f"{x}=excluded.{x}" for x in cols]
        qs = f"""
        INSERT INTO accounts ({",".join(cols)}) VALUES ({",".join([f":{x}" for x in cols])})
        ON CONFLICT(username) DO UPDATE SET {",".join(updates)}
        """
        await execute(self._db_file, qs, data)

    async def login(self, account: Account):
        try:
            await login(account, cfg=self._login_config)
            account.error_msg = None
            logger.info(f"Logged in to {account.username} successfully")
            return True
        except HTTPStatusError as e:
            rep = e.response
            account.active = False
            account.error_msg = f"HTTPStatusError {rep.status_code}: {rep.text}"[:200]
            logger.error(f"Failed to login '{account.username}': {rep.status_code} - {rep.text}")
            return False
        except Exception as e:
            account.active = False
            if not account.error_msg:
                account.error_msg = f"{type(e).__name__}: {e}"[:200]
            logger.error(f"Failed to login '{account.username}': {e}")
            return False
        finally:
            await self.save(account)

    async def login_all(self, usernames: list[str] | None = None):
        params = None
        if usernames is None:
            qs = "SELECT * FROM accounts WHERE active = false AND error_msg IS NULL"
        else:
            if not usernames:
                return {"total": 0, "success": 0, "failed": 0}

            placeholders, params = self._usernames_where(usernames)
            qs = f"SELECT * FROM accounts WHERE username IN ({placeholders})"

        rs = await fetchall(self._db_file, qs, params)
        accounts = [Account.from_rs(rs) for rs in rs]
        accounts = [account for account in accounts if account.login_method == "password"]
        # await asyncio.gather(*[login(x) for x in self.accounts])

        counter = {"total": len(accounts), "success": 0, "failed": 0}
        for i, x in enumerate(accounts, start=1):
            logger.info(f"[{i}/{len(accounts)}] Logging in {x.username} - {x.email}")
            status = await self.login(x)
            counter["success" if status else "failed"] += 1
        return counter

    async def relogin(self, usernames: str | list[str]):
        usernames = usernames if isinstance(usernames, list) else [usernames]
        usernames = list(set(usernames))
        if not usernames:
            logger.warning("No usernames provided")
            return

        placeholders, params = self._usernames_where(usernames)
        params["user_agent"] = UserAgent().safari
        qs = f"""
        UPDATE accounts SET
            active = false,
            locks = json_object(),
            last_used = NULL,
            error_msg = NULL,
            headers = json_object(),
            cookies = json_object(),
            user_agent = :user_agent
        WHERE username IN ({placeholders}) AND password != '_'
        """

        await execute(self._db_file, qs, params)
        await self.login_all(usernames)

    async def relogin_failed(self):
        qs = "SELECT username FROM accounts WHERE active = false AND error_msg IS NOT NULL"
        rs = await fetchall(self._db_file, qs)
        await self.relogin([x["username"] for x in rs])

    async def reset_locks(self):
        """Lift every lock and end every refusal streak, so a returned account starts at the first step."""
        qs = f"""
        UPDATE accounts SET
            locks = json_object(),
            stats = json_remove(stats, '$."{REFUSALS_STATS_KEY}"')
        """
        await execute(self._db_file, qs)

    async def set_active(self, username: str, active: bool):
        qs = "UPDATE accounts SET active = :active WHERE username = :username"
        await execute(self._db_file, qs, {"username": username, "active": active})

    async def lock_until(self, username: str, queue: str, unlock_at: int, req_count=0):
        qs = f"""
        UPDATE accounts SET
            locks = json_set(locks, '$.{queue}', datetime({unlock_at}, 'unixepoch')),
            stats = json_set(stats, '$.{queue}', COALESCE(json_extract(stats, '$.{queue}'), 0) + {req_count}),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE username = :username
        """
        await execute(self._db_file, qs, {"username": username})

    async def unlock(self, username: str, queue: str, req_count=0):
        qs = f"""
        UPDATE accounts SET
            locks = json_remove(locks, '$.{queue}'),
            stats = json_set(stats, '$.{queue}', COALESCE(json_extract(stats, '$.{queue}'), 0) + {req_count}),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE username = :username
        """
        await execute(self._db_file, qs, {"username": username})

    async def record_refusal(self, username: str, queue: str) -> int:
        """Counts one more consecutive refusal of `queue` for the account and returns the new count."""
        path = f'$."{REFUSALS_STATS_KEY}"."{queue}"'
        qs = f"""
        UPDATE accounts SET stats = json_set(stats, '{path}', COALESCE(json_extract(stats, '{path}'), 0) + 1)
        WHERE username = :username
        """
        await execute(self._db_file, qs, {"username": username})
        rs = await fetchone(
            self._db_file,
            f"SELECT json_extract(stats, '{path}') FROM accounts WHERE username = :username",
            {"username": username},
        )
        return int(rs[0]) if rs and rs[0] else 1

    async def first_used(self, username: str) -> datetime:
        """When the account sent its first budgeted request; stored once, on the first call (the warm-up clock)."""
        path = f'$."{FIRST_USED_STATS_KEY}"'
        qs = f"""
        UPDATE accounts SET stats = json_set(stats, '{path}', :now)
        WHERE username = :username AND json_extract(stats, '{path}') IS NULL
        """
        await execute(self._db_file, qs, {"username": username, "now": utc.now().isoformat()})
        rs = await fetchone(
            self._db_file,
            f"SELECT json_extract(stats, '{path}') FROM accounts WHERE username = :username",
            {"username": username},
        )
        return utc.from_iso(rs[0]) if rs and rs[0] else utc.now()

    async def count_request(self, username: str, queue: str, cap: int | None) -> int | None:
        """Counts one request of the account on `queue` for today (UTC) before it is sent; returns the new count.

        One UPDATE checks the cap and increments, so two processes cannot both pass a full cap. Returns None and
        counts nothing when today's count has already reached `cap`. A new UTC day starts again at 1.
        """
        path = f'$."{DAILY_STATS_KEY}"."{queue}"'
        same_day = f"json_extract(stats, '{path}.day') = :day"
        count = f"COALESCE(json_extract(stats, '{path}.count'), 0)"
        tx = uuid.uuid4().hex
        qs = f"""
        UPDATE accounts SET
            stats = json_set(
                stats, '{path}',
                json_object('day', :day, 'count', CASE WHEN {same_day} THEN {count} + 1 ELSE 1 END)
            ),
            _tx = :tx
        WHERE username = :username
            AND (:cap IS NULL OR json_extract(stats, '{path}.day') IS NOT :day OR {count} < :cap)
        """
        params = {"username": username, "day": utc_day(utc.now()), "cap": cap, "tx": tx}
        await execute(self._db_file, qs, params)
        rs = await fetchone(
            self._db_file,
            f"SELECT json_extract(stats, '{path}.count') FROM accounts WHERE _tx = :tx",
            {"tx": tx},
        )
        return int(rs[0]) if rs and rs[0] is not None else None

    async def reset_refusals(self, username: str, queue: str) -> None:
        """Ends the account's refusal streak on `queue`; writes nothing when there is none."""
        path = f'$."{REFUSALS_STATS_KEY}"."{queue}"'
        qs = f"""
        UPDATE accounts SET stats = json_remove(stats, '{path}')
        WHERE username = :username AND json_extract(stats, '{path}') IS NOT NULL
        """
        await execute(self._db_file, qs, {"username": username})

    async def _get_and_lock(self, queue: str, condition: str):
        # if space in condition, it's a subquery, otherwise it's username
        condition = f"({condition})" if " " in condition else f"'{condition}'"

        if int(sqlite3.sqlite_version_info[1]) >= 35:
            qs = f"""
            UPDATE accounts SET
                locks = json_set(locks, '$.{queue}', datetime('now', '+15 minutes')),
                last_used = datetime({utc.ts()}, 'unixepoch')
            WHERE username = {condition}
            RETURNING *
            """
            rs = await fetchone(self._db_file, qs)
        else:
            tx = uuid.uuid4().hex
            qs = f"""
            UPDATE accounts SET
                locks = json_set(locks, '$.{queue}', datetime('now', '+15 minutes')),
                last_used = datetime({utc.ts()}, 'unixepoch'),
                _tx = '{tx}'
            WHERE username = {condition}
            """
            await execute(self._db_file, qs)

            qs = f"SELECT * FROM accounts WHERE _tx = '{tx}'"
            rs = await fetchone(self._db_file, qs)

        return Account.from_rs(rs) if rs else None

    async def get_for_queue(self, queue: str):
        q = f"""
        SELECT username FROM accounts
        WHERE active = true AND (
            json_extract(locks, '$."{GLOBAL_LOCK_QUEUE}"') IS NULL
            OR datetime(json_extract(locks, '$."{GLOBAL_LOCK_QUEUE}"')) < datetime('now')
        ) AND (
            locks IS NULL
            OR json_extract(locks, '$.{queue}') IS NULL
            OR datetime(json_extract(locks, '$.{queue}')) < datetime('now')
        )
        ORDER BY {self._order_by}
        LIMIT 1
        """

        return await self._get_and_lock(queue, q)

    async def get_for_queue_or_wait(self, queue: str) -> Account | None:
        loop = asyncio.get_running_loop()
        deadline = None if self._wait_timeout is None else loop.time() + self._wait_timeout
        msg_shown = False
        while True:
            account = await self.get_for_queue(queue)
            if account is not None:
                if msg_shown:
                    logger.info(f"Continuing with account {account.username} on queue {queue}")
                return account

            raise_no_account = self._raise_when_no_account or get_env_bool(
                "TWS_RAISE_WHEN_NO_ACCOUNT"
            )
            nat = await self.next_available_at(queue)
            no_active = not nat
            timed_out = deadline is not None and loop.time() >= deadline
            if deadline is not None and not timed_out:
                wait_s = await self.seconds_until_available(queue)
                # Every account is held for longer than the wait could last, e.g. quarantined from search.
                if wait_s is not None and wait_s > max(
                    deadline - loop.time(), IN_USE_LOCK_SECONDS + 60
                ):
                    # An account in use may have come back since the first look; its lock is gone then.
                    account = await self.get_for_queue(queue)
                    if account is not None:
                        return account
                    timed_out = True
            give_up = no_active or self._wait_timeout is None or timed_out

            if give_up:
                if raise_no_account:
                    raise NoAccountError(f"No account available for queue {queue}")
                if no_active:
                    logger.warning("No active accounts. Stopping...")
                    return None
                if self._wait_timeout is not None:
                    return None

            if not msg_shown:
                logger.info(f'No account available for queue "{queue}". Next available at {nat}')
                msg_shown = True

            remaining = None if deadline is None else max(deadline - loop.time(), 0)
            interval = (
                self._wait_interval if remaining is None else min(self._wait_interval, remaining)
            )
            await asyncio.sleep(interval)

    async def _earliest_unlock(self, queue: str) -> str | None:
        """When the first locked active account is free for `queue`: per account the later of its `queue` lock and
        its global lock (both must have ended), then the earliest across accounts.

        Locks come in two formats (save writes ISO with a T and +00:00, lock_until 'YYYY-MM-DD HH:MM:SS'), so
        every comparison goes through julianday()/datetime(), never the text."""
        qs = f"""
        SELECT datetime(MIN(account_until)) AS lock_until FROM (
            SELECT MAX(julianday(lock_entry.value)) AS account_until
            FROM accounts
            JOIN json_each(locks) AS lock_entry
            WHERE active = true
                AND lock_entry.key IN ('{queue}', '{GLOBAL_LOCK_QUEUE}')
                AND lock_entry.value IS NOT NULL
            GROUP BY accounts.username
        )
        """
        rs = await fetchone(self._db_file, qs)
        return rs[0] if rs and rs[0] else None

    async def seconds_until_available(self, queue: str) -> float | None:
        """Seconds until the first locked active account is free for `queue` (see `_earliest_unlock`)."""
        lock_until = await self._earliest_unlock(queue)
        if lock_until is None:
            return None
        return max(0.0, (utc.from_iso(lock_until) - utc.now()).total_seconds())

    async def next_available_at(self, queue: str):
        lock_until = await self._earliest_unlock(queue)
        if lock_until is not None:
            now, trg = utc.now(), utc.from_iso(lock_until)
            if trg < now:
                return "now"

            at_local = datetime.now() + (trg - now)
            return at_local.strftime("%H:%M:%S")

        return None

    async def mark_inactive(
        self, username: str, error_msg: str | None, *, queue: str | None = None, req_count=0
    ):
        qs = """
        UPDATE accounts SET active = false, error_msg = :error_msg,
            stats = CASE WHEN :queue_path IS NULL THEN stats ELSE
                json_set(stats, :queue_path, COALESCE(json_extract(stats, :queue_path), 0) + :req_count)
            END
        WHERE username = :username
        """
        await execute(
            self._db_file,
            qs,
            {
                "username": username,
                "error_msg": error_msg,
                "queue_path": f"$.{queue}" if queue is not None else None,
                "req_count": req_count,
            },
        )

    async def stats(self):
        def locks_count(queue: str):
            return f"""
            SELECT COUNT(*) FROM accounts
            WHERE json_extract(locks, '$.{queue}') IS NOT NULL
                AND datetime(json_extract(locks, '$.{queue}')) > datetime('now')
            """

        qs = "SELECT DISTINCT(f.key) as k from accounts, json_each(locks) f"
        rs = await fetchall(self._db_file, qs)
        gql_ops = [x["k"] for x in rs]

        config = [
            ("total", "SELECT COUNT(*) FROM accounts"),
            ("active", "SELECT COUNT(*) FROM accounts WHERE active = true"),
            ("inactive", "SELECT COUNT(*) FROM accounts WHERE active = false"),
            *[(f"locked_{x}", locks_count(x)) for x in gql_ops],
        ]

        qs = f"SELECT {','.join([f'({q}) as {k}' for k, q in config])}"
        rs = await fetchone(self._db_file, qs)
        return dict(rs) if rs else {}

    async def accounts_info(self):
        accounts = await self.get_all()

        items: list[AccountInfo] = []
        for x in accounts:
            item: AccountInfo = {
                "username": x.username,
                "logged_in": x.has_session,
                "login_method": x.login_method,
                "active": x.active,
                "last_used": x.last_used,
                "total_req": sum(x.stats.values()),
                "error_msg": str(x.error_msg)[0:60],
            }
            items.append(item)

        old_time = datetime(1970, 1, 1).replace(tzinfo=timezone.utc)
        items = sorted(items, key=lambda x: x["username"].lower())
        items = sorted(
            items,
            key=lambda x: x["last_used"] or old_time if x["total_req"] > 0 else old_time,
            reverse=True,
        )
        items = sorted(items, key=lambda x: x["active"], reverse=True)
        # items = sorted(items, key=lambda x: x["total_req"], reverse=True)
        return items
