#!/usr/bin/env python3

import argparse
import asyncio
import io
import json
import sqlite3
from collections.abc import Awaitable, Callable
from importlib.metadata import version

import httpx

from .accounts_pool import AccountsPool
from .api import API
from .db import get_sqlite_version
from .logger import logger, set_log_level
from .login import LoginConfig
from .models import Tweet, User
from .queue_client import ApiFeatureUpdateRequiredError, UnexpectedApiError
from .utils import print_table

PoolCommandHandler = Callable[[AccountsPool, argparse.Namespace], Awaitable[None]]
ARG_NAMES = ("query", "tweet_id", "user_id", "username", "list_id", "trend_id")


class CustomHelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=30, width=120)


def get_fn_arg(args):
    for name in ARG_NAMES:
        if hasattr(args, name):
            return name, getattr(args, name)

    logger.error(f"Missing argument: {ARG_NAMES}")
    raise SystemExit(1)


def to_str(doc: httpx.Response | Tweet | User | None) -> str:
    if doc is None:
        return "Not Found. See --raw for more details."

    tmp = doc.json()
    return tmp if isinstance(tmp, str) else json.dumps(tmp, default=str)


async def _cmd_version() -> None:
    print(f"twscrape: {version('twscrape')}")
    print(f"SQLite runtime: {sqlite3.sqlite_version} ({await get_sqlite_version()})")


async def _cmd_accounts(pool: AccountsPool, _: argparse.Namespace) -> None:
    print_table([dict(x) for x in await pool.accounts_info()])


async def _cmd_stats(pool: AccountsPool, _: argparse.Namespace) -> None:
    rep = await pool.stats()
    total, active, inactive = rep["total"], rep["active"], rep["inactive"]

    rows = [
        {"queue": key, "locked": value, "available": max(active - value, 0)}
        for key, value in rep.items()
        if key.startswith("locked") and value > 0
    ]
    rows = sorted(rows, key=lambda x: x["locked"], reverse=True)
    print_table(rows, hr_after=True)
    print(f"Total: {total} - Active: {active} - Inactive: {inactive}")


async def _cmd_add_accounts(pool: AccountsPool, args: argparse.Namespace) -> None:
    await pool.load_from_file(args.file_path, args.line_format)
    print("\nNow run:\ntwscrape login_accounts")


async def _cmd_delete_accounts(pool: AccountsPool, args: argparse.Namespace) -> None:
    await pool.delete_accounts(args.usernames)


async def _cmd_login_accounts(pool: AccountsPool, _: argparse.Namespace) -> None:
    print(await pool.login_all())


async def _cmd_relogin_failed(pool: AccountsPool, _: argparse.Namespace) -> None:
    await pool.relogin_failed()


async def _cmd_relogin(pool: AccountsPool, args: argparse.Namespace) -> None:
    await pool.relogin(args.usernames)


async def _cmd_reset_locks(pool: AccountsPool, _: argparse.Namespace) -> None:
    await pool.reset_locks()


async def _cmd_delete_inactive(pool: AccountsPool, _: argparse.Namespace) -> None:
    await pool.delete_inactive()


POOL_COMMANDS: dict[str, PoolCommandHandler] = {
    "accounts": _cmd_accounts,
    "stats": _cmd_stats,
    "add_accounts": _cmd_add_accounts,
    "del_accounts": _cmd_delete_accounts,
    "login_accounts": _cmd_login_accounts,
    "relogin_failed": _cmd_relogin_failed,
    "relogin": _cmd_relogin,
    "reset_locks": _cmd_reset_locks,
    "delete_inactive": _cmd_delete_inactive,
}


async def _print_api_response(fn: Callable, value: str | int, limit: int | None) -> None:
    if limit is None:
        print(to_str(await fn(value)))
        return

    async for doc in fn(value, limit=limit):
        print(to_str(doc))


async def _run_api_command(api: API, args: argparse.Namespace) -> None:
    fn_name = args.command + "_raw" if args.raw else args.command
    fn = getattr(api, fn_name, None)
    if fn is None:
        logger.error(f"Unknown command: {args.command}")
        raise SystemExit(1)

    _, value = get_fn_arg(args)
    limit = getattr(args, "limit", None)
    await _print_api_response(fn, value, limit)


async def main(args):
    if args.debug:
        set_log_level("DEBUG")

    if args.command == "version":
        await _cmd_version()
        return

    login_config = LoginConfig(getattr(args, "email_first", False), getattr(args, "manual", False))
    pool = AccountsPool(args.db, login_config=login_config)

    handler = POOL_COMMANDS.get(args.command)
    if handler is not None:
        await handler(pool, args)
        return

    api = API(pool, debug=args.debug)
    await _run_api_command(api, args)


def custom_help(p):
    buffer = io.StringIO()
    p.print_help(buffer)
    msg = buffer.getvalue()

    cmd = msg.split("positional arguments:")[1].strip().split("\n")[0]
    msg = msg.replace("positional arguments:", "commands:")
    msg = [x for x in msg.split("\n") if cmd not in x and "..." not in x]
    msg[0] = f"{msg[0]} <command> [...]"

    i = 0
    for i, line in enumerate(msg):
        if line.strip().startswith("search"):
            break

    msg.insert(i, "")
    msg.insert(i + 1, "search commands:")

    print("\n".join(msg))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False, formatter_class=CustomHelpFormatter)
    p.add_argument("--db", default="accounts.db", help="Accounts database file")
    p.add_argument("--debug", action="store_true", help="Enable debug mode")
    subparsers = p.add_subparsers(dest="command")

    def c_one(name: str, msg: str, a_name: str, a_msg: str, a_type: type = str):
        p = subparsers.add_parser(name, help=msg)
        p.add_argument(a_name, help=a_msg, type=a_type)
        p.add_argument("--raw", action="store_true", help="Print raw response")
        return p

    def c_lim(name: str, msg: str, a_name: str, a_msg: str, a_type: type = str):
        p = c_one(name, msg, a_name, a_msg, a_type)
        p.add_argument("--limit", type=int, default=-1, help="Max tweets to retrieve")
        return p

    subparsers.add_parser("version", help="Show version")
    subparsers.add_parser("accounts", help="List all accounts")
    subparsers.add_parser("stats", help="Get current usage stats")

    add_accounts = subparsers.add_parser("add_accounts", help="Add accounts")
    add_accounts.add_argument("file_path", help="File with accounts")
    add_accounts.add_argument("line_format", help="args of Pool.add_account splited by same delim")

    del_accounts = subparsers.add_parser("del_accounts", help="Delete accounts")
    del_accounts.add_argument("usernames", nargs="+", default=[], help="Usernames to delete")

    login_cmd = subparsers.add_parser("login_accounts", help="Login accounts")
    relogin = subparsers.add_parser("relogin", help="Re-login selected accounts")
    relogin.add_argument("usernames", nargs="+", default=[], help="Usernames to re-login")
    re_failed = subparsers.add_parser("relogin_failed", help="Retry login for failed accounts")

    login_commands = [login_cmd, relogin, re_failed]
    for cmd in login_commands:
        cmd.add_argument("--email-first", action="store_true", help="Check email first")
        cmd.add_argument("--manual", action="store_true", help="Enter email code manually")

    subparsers.add_parser("reset_locks", help="Reset all locks")
    subparsers.add_parser("delete_inactive", help="Delete inactive accounts")

    c_lim("search", "Search for tweets", "query", "Search query")
    c_one("tweet_details", "Get tweet details", "tweet_id", "Tweet ID", int)
    c_lim("tweet_replies", "Get replies  of a tweet", "tweet_id", "Tweet ID", int)
    c_lim("retweeters", "Get retweeters of a tweet", "tweet_id", "Tweet ID", int)
    c_one("user_by_id", "Get user data by ID", "user_id", "User ID", int)
    c_one("user_by_login", "Get user data by username", "username", "Username")
    c_lim("following", "Get user following", "user_id", "User ID", int)
    c_lim("followers", "Get user followers", "user_id", "User ID", int)
    # https://x.com/xDaily/status/1701694747767648500
    c_lim("verified_followers", "Get user verified followers", "user_id", "User ID", int)
    c_lim("subscriptions", "Get user subscriptions", "user_id", "User ID", int)
    c_lim("user_tweets", "Get user tweets", "user_id", "User ID", int)
    c_lim("user_tweets_and_replies", "Get user tweets and replies", "user_id", "User ID", int)
    c_lim("user_media", "Get user's media", "user_id", "User ID", int)
    c_lim("list_timeline", "Get tweets from list", "list_id", "List ID", int)
    c_lim("trends", "Get trends", "trend_id", "Trend ID or name", str)

    return p


def run():
    p = build_parser()
    args = p.parse_args()
    if args.command is None:
        return custom_help(p)

    try:
        asyncio.run(main(args))
    except ApiFeatureUpdateRequiredError as e:
        logger.error(str(e))
        raise SystemExit(1)
    except UnexpectedApiError as e:
        logger.error(str(e))
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass
