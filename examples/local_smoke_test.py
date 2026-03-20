#!/usr/bin/env python3

import argparse
import asyncio
import getpass
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from twscrape import API, AccountsPool, set_log_level
from twscrape.login import LoginConfig
from twscrape.search_queries import build_stock_cashtag_query
from twscrape.utils import parse_cookies

DEFAULT_DB = ".local/smoke.db"
DEFAULT_PROBE_USER = "xdevelopers"
DEFAULT_QUERY = "from:xdevelopers"


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run a small live smoke test against X using a separate local DB."
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB for the smoke test")
    parser.add_argument(
        "--username",
        default=None,
        help="X account username. Can also be provided via TWS_USERNAME",
    )
    parser.add_argument(
        "--cookies",
        default=None,
        help="Cookie string/JSON/base64. Can also be provided via TWS_COOKIES",
    )
    parser.add_argument(
        "--prompt-cookies",
        action="store_true",
        help="Prompt for auth_token and ct0 without echoing them",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Login with username/password instead of pre-existing cookies",
    )
    parser.add_argument("--email", default=None, help="Account email or TWS_EMAIL")
    parser.add_argument("--password", default=None, help="Account password or TWS_PASSWORD")
    parser.add_argument(
        "--email-password",
        default=None,
        help="Mailbox password for IMAP login or TWS_EMAIL_PASSWORD",
    )
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Enter email verification code manually if X requests it",
    )
    parser.add_argument(
        "--probe-user",
        default=DEFAULT_PROBE_USER,
        help="Username used for a simple user lookup",
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="Search query used for the live smoke test",
    )
    parser.add_argument(
        "--ticker",
        default=None,
        help="Build a stock-style cashtag query like the Reddit-Sentiment X scraper",
    )
    parser.add_argument(
        "--hours-back",
        type=int,
        default=24,
        help="Used with --ticker to set the since: window",
    )
    parser.add_argument(
        "--min-faves",
        type=int,
        default=2,
        help="Used with --ticker to mirror the Reddit-Sentiment X scraper threshold",
    )
    parser.add_argument("--limit", type=int, default=5, help="Max tweets to fetch for the search")
    parser.add_argument("--proxy", default=None, help="Optional proxy URL")
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Delete the smoke-test DB before preparing the account",
    )
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging")
    return parser


async def seed_account(api: API, username: str, cookies: str, proxy: str | None):
    cookie_map = parse_cookies(cookies)
    missing = [key for key in ("ct0", "auth_token") if key not in cookie_map]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Cookie payload is missing required keys: {joined}")

    account = await api.pool.get_account(username)
    if account is None:
        await api.pool.add_account(
            username=username,
            password="_",
            email=f"{username}@local.invalid",
            email_password="_",
            cookies=cookies,
            proxy=proxy,
        )
        return "created"

    account.cookies = cookie_map
    account.active = True
    account.error_msg = None
    account.headers = {}
    account.locks = {}
    account.stats = {}
    if proxy is not None:
        account.proxy = proxy
    await api.pool.save(account)
    return "updated"


def prompt_if_missing(value: str | None, label: str, *, secret=False):
    if value:
        return value
    return getpass.getpass(f"{label}: ") if secret else input(f"{label}: ").strip()


async def login_account(
    pool: AccountsPool,
    username: str,
    password: str,
    email: str,
    email_password: str,
    proxy: str | None,
):
    account = await pool.get_account(username)
    if account is None:
        await pool.add_account(
            username=username,
            password=password,
            email=email,
            email_password=email_password,
            proxy=proxy,
        )
        account = await pool.get(username)
    else:
        account.password = password
        account.email = email
        account.email_password = email_password
        account.proxy = proxy
        account.active = False
        account.error_msg = None
        account.headers = {}
        account.cookies = {}
        account.locks = {}
        account.stats = {}
        await pool.save(account)

    ok = await pool.login(account)
    if not ok:
        refreshed = await pool.get(username)
        raise SystemExit(f"Login failed for @{username}: {refreshed.error_msg or 'unknown error'}")

    print(f"Logged in successfully and stored session in {pool._db_file}")


def shorten(text: str, limit: int = 120):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


async def run():
    parser = build_parser()
    args = parser.parse_args()

    username = args.username or os.getenv("TWS_USERNAME")
    cookies = args.cookies or os.getenv("TWS_COOKIES")
    email = args.email or os.getenv("TWS_EMAIL")
    password = args.password or os.getenv("TWS_PASSWORD")
    email_password = args.email_password or os.getenv("TWS_EMAIL_PASSWORD")
    proxy = args.proxy or os.getenv("TWS_PROXY")

    set_log_level("DEBUG" if args.debug else "WARNING")

    db_path = Path(args.db)
    if args.reset_db and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    login_config = LoginConfig(manual=args.manual_login)
    pool = AccountsPool(str(db_path), login_config=login_config, raise_when_no_account=True)
    api = API(pool, debug=args.debug, proxy=proxy, raise_when_no_account=True)

    if args.prompt_cookies and cookies is None:
        username = username or input("X username: ").strip()
        auth_token = getpass.getpass("auth_token: ")
        ct0 = getpass.getpass("ct0: ")
        cookies = f"auth_token={auth_token}; ct0={ct0}"

    if cookies is not None:
        if username is None:
            raise SystemExit(
                "Missing username. Set --username or TWS_USERNAME together with cookies."
            )

        action = await seed_account(api, username, cookies, proxy)
        print(f"Prepared smoke-test account ({action}) in {db_path}")
    elif args.login:
        username = prompt_if_missing(username, "X username")
        email = prompt_if_missing(email, "X email")
        password = prompt_if_missing(password, "X password", secret=True)
        if args.manual_login:
            email_password = email_password or "_"
        else:
            email_password = prompt_if_missing(email_password, "Email password", secret=True)

        await login_account(api.pool, username, password, email, email_password, proxy)
    else:
        accounts = await api.pool.get_all()
        if not accounts:
            raise SystemExit(
                "No accounts available. Set cookies, use --login, or pre-populate the DB."
            )

    query = args.query
    if args.ticker:
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours_back)
        query = build_stock_cashtag_query(
            args.ticker,
            since,
            min_faves=args.min_faves,
        )
        print(f"ticker query: {query}")

    user = await api.user_by_login(args.probe_user)
    if user is None:
        raise SystemExit(f"user_by_login failed for @{args.probe_user}")

    print(f"user_by_login OK: @{user.username} ({user.id})")

    tweets = []
    async for tweet in api.search(query, limit=args.limit):
        tweets.append(tweet)

    if not tweets:
        raise SystemExit(f'search returned 0 tweets for query "{query}"')

    print(f'search OK: {len(tweets)} tweets for "{query}"')
    for idx, tweet in enumerate(tweets, start=1):
        print(f"{idx:02d}. {tweet.id} @{tweet.user.username}: {shorten(tweet.rawContent)}")


if __name__ == "__main__":
    asyncio.run(run())
