# twscrape

<div align="center">

[<img src="https://badges.ws/github/license/alexander-schneider/twscrape" alt="license" />](https://github.com/alexander-schneider/twscrape/blob/main/LICENSE)
[<img src="https://badges.ws/badge/-/buy%20me%20a%20coffee/ff813f?icon=buymeacoffee&label" alt="donate" />](https://buymeacoffee.com/vladkens)

</div>

Twitter GraphQL API implementation with [SNScrape](https://github.com/JustAnotherArchivist/snscrape) data models.

This repository is an Adanos-maintained fork of the original
[`vladkens/twscrape`](https://github.com/vladkens/twscrape).
The original project remains the historical upstream reference.
This fork is the active source of truth for fixes used by Adanos projects.
Please open issues and pull requests in this repository.

<div align="center">
  <img src=".github/example.png" alt="example of cli usage" height="400px">
</div>

## Install

Do not use:

```bash
pip install twscrape
```

That installs the upstream PyPI package and not this fork.

Use this repository directly instead:

```bash
pip install git+https://github.com/alexander-schneider/twscrape.git
```

For local development:

```bash
git clone https://github.com/alexander-schneider/twscrape.git
cd twscrape
pip install -e .[dev]
```

## Local Smoke Test

For local development, split the checks into two layers:

1. Offline regression tests

```bash
pip install -e .[dev]
make test
```

2. Live search verification against X with a cookie-authenticated account

This is the important end-to-end check for Adanos usage. It uses a real account
and verifies that X search still returns tweets.

```bash
TWS_USERNAME="your_account" \
TWS_COOKIES='auth_token=...; ct0=...' \
make test-live
```

By default the live test uses the Reddit-Sentiment style cashtag query builder
with a recent ticker window (`NVDA`, `24h`, `min_faves:2`). You can override the
probe with environment variables such as `TWS_LIVE_TICKER`, `TWS_LIVE_LIMIT`, or
`TWS_LIVE_QUERY`.

To make this work reliably from Codex automations or other separate worktrees,
seed a shared local session DB once:

```bash
make test-live-seed-prompt
```

This stores the authenticated session at
`~/.local/share/twscrape/live-check.db`, outside the Git worktree.
After that, `make test-live` and `make check` can reuse the same local account
without passing cookies every time.

For a full local verification pass, run:

```bash
TWS_USERNAME="your_account" \
TWS_COOKIES='auth_token=...; ct0=...' \
make check
```

3. Manual smoke test against X with a cookie-authenticated account

Cookie auth is the most reliable local path. Login flows through username/password/email are much more fragile because X can change challenges, require email confirmation, or trip IP-based checks.

```bash
TWS_USERNAME="your_account" \
TWS_COOKIES='auth_token=...; ct0=...' \
make smoke-reset
```

This uses a separate SQLite DB at `.local/smoke.db`, prepares one account, then checks:

- `user_by_login(xdevelopers)`
- `search("from:xdevelopers")`

If you do not want cookies in shell history, use the hidden prompt mode:

```bash
make smoke-prompt
```

After the first successful setup, rerun the live check with the stored local session:

```bash
make smoke
```

For the stock/cashtag search pattern used in `Reddit-Sentiment`, run:

```bash
make smoke-stock TICKER=NVDA
```

That builds the same style of query used there:

```text
$NVDA min_faves:2 lang:en until:YYYY-MM-DD since:YYYY-MM-DD -filter:links
```

You can change the search window and threshold:

```bash
make smoke-stock TICKER=TSLA HOURS=12 MIN_FAVES=5 LIMIT=10
```

For first-time setup without exposing cookies in shell history:

```bash
make smoke-stock-prompt TICKER=NVDA
```

You can customize the probe:

```bash
python examples/local_smoke_test.py \
  --reset-db \
  --username "your_account" \
  --cookies 'auth_token=...; ct0=...' \
  --probe-user "OpenAI" \
  --query "from:OpenAI" \
  --limit 3
```

If you need a proxy, pass `--proxy ...` or set `TWS_PROXY`.

Password login is supported for local debugging, but should be treated as fallback only:

```bash
python examples/local_smoke_test.py --reset-db --login --manual-login
```

The script will prompt for credentials and, if needed, for the email verification code. Sessions are stored only in `.local/smoke.db`, which is ignored by Git.

## Features
- Support both Search & GraphQL Twitter API
- Async/Await functions (can run multiple scrapers in parallel at the same time)
- Login flow (with receiving verification code from email)
- Saving/restoring account sessions
- Raw Twitter API responses & SNScrape models
- Automatic account switching to smooth Twitter API rate limits

## Usage

This project requires authorized X/Twitter accounts to work with the API. You have two options:

1. **Create Your Own Account**: While you can register a new account on X/Twitter yourself, it can be difficult due to strict verification processes and high ban rates.

2. **Use Ready Accounts**: For immediate access, you can get ready-to-use accounts with cookies from [our recommended provider](https://kutt.it/ueeM5f). Cookie-based accounts typically have fewer login issues.

For optimal performance and to avoid IP-based restrictions, we also recommend using proxies from [our provider](https://kutt.it/eb3rXk).

**Disclaimer**: While X/Twitter's Terms of Service discourage using multiple accounts, this is a common practice for data collection and research purposes. Use responsibly and at your own discretion.

```python
import asyncio
from twscrape import API, gather
from twscrape.logger import set_log_level

async def main():
    api = API()  # or API("path-to.db") – default is `accounts.db`

    # ADD ACCOUNTS (for CLI usage see next readme section)

    # Option 1. Adding account with cookies (more stable)
    cookies = "abc=12; ct0=xyz"  # or '{"abc": "12", "ct0": "xyz"}'
    await api.pool.add_account("user3", "pass3", "u3@mail.com", "mail_pass3", cookies=cookies)

    # Option2. Adding account with login / password (less stable)
    # email login / password required to receive the verification code via IMAP protocol
    # (not all email providers are supported, e.g. ProtonMail)
    await api.pool.add_account("user1", "pass1", "u1@example.com", "mail_pass1")
    await api.pool.add_account("user2", "pass2", "u2@example.com", "mail_pass2")
    await api.pool.login_all() # try to login to receive account cookies

    # API USAGE

    # search (latest tab)
    await gather(api.search("elon musk", limit=20))  # list[Tweet]
    # change search tab (product), can be: Top, Latest (default), Media
    await gather(api.search("elon musk", limit=20, kv={"product": "Top"}))

    # tweet info
    tweet_id = 20
    await api.tweet_details(tweet_id)  # Tweet
    await gather(api.retweeters(tweet_id, limit=20))  # list[User]

    # Note: this method have small pagination from X side, like 5 tweets per query
    await gather(api.tweet_replies(tweet_id, limit=20))  # list[Tweet]

    # get user by login
    user_login = "xdevelopers"
    await api.user_by_login(user_login)  # User

    # user info
    user_id = 2244994945
    await api.user_by_id(user_id)  # User
    await gather(api.following(user_id, limit=20))  # list[User]
    await gather(api.followers(user_id, limit=20))  # list[User]
    await gather(api.verified_followers(user_id, limit=20))  # list[User]
    await gather(api.subscriptions(user_id, limit=20))  # list[User]
    await gather(api.user_tweets(user_id, limit=20))  # list[Tweet]
    await gather(api.user_tweets_and_replies(user_id, limit=20))  # list[Tweet]
    await gather(api.user_media(user_id, limit=20))  # list[Tweet]

    # list info
    await gather(api.list_timeline(list_id=123456789))

    # trends
    await gather(api.trends("news"))  # list[Trend]
    await gather(api.trends("sport"))  # list[Trend]
    await gather(api.trends("VGltZWxpbmU6DAC2CwABAAAACHRyZW5kaW5nAAA"))  # list[Trend]

    # NOTE 1: gather is a helper function to receive all data as list, FOR can be used as well:
    async for tweet in api.search("elon musk"):
        print(tweet.id, tweet.user.username, tweet.rawContent)  # tweet is `Tweet` object

    # NOTE 2: all methods have `raw` version (returns `httpx.Response` object):
    async for rep in api.search_raw("elon musk"):
        print(rep.status_code, rep.json())  # rep is `httpx.Response` object

    # change log level, default info
    set_log_level("DEBUG")

    # Tweet & User model can be converted to regular dict or json, e.g.:
    doc = await api.user_by_id(user_id)  # User
    doc.dict()  # -> python dict
    doc.json()  # -> json string

if __name__ == "__main__":
    asyncio.run(main())
```

### Handling transient search interruptions

Search requests can occasionally fail with a transient X-side
`ServiceUnavailable` response even when the account is still healthy. The fork
exposes this as a typed `ServiceUnavailableError` so callers can retry that path
without conflating it with generic API drift or account bans.

```python
from twscrape import API, ServiceUnavailableError, gather

async def resilient_search():
    api = API()

    try:
        return await gather(api.search("$NVDA lang:en", limit=20))
    except ServiceUnavailableError:
        # Retry once, back off, or keep already-collected partials in your app.
        return []
```

### Stopping iteration with break

In order to correctly release an account in case of `break` in a loop, a special syntax must be used. Otherwise, Python's event loop will release the lock on the account sometime in the future. See explanation [here](https://github.com/vladkens/twscrape/issues/27#issuecomment-1623395424).

```python
from contextlib import aclosing

async with aclosing(api.search("elon musk")) as gen:
    async for tweet in gen:
        if tweet.id < 200:
            break
```

## CLI

### Get help on CLI commands

```sh
# show all commands
twscrape

# help on a specific command
twscrape search --help
```

### Add accounts

To add accounts use `add_accounts` command. Command syntax is:
```sh
twscrape add_accounts <file_path> <line_format>
```

Where:
`<line_format>` is the format of each line in the accounts file, split by a delimiter. Possible tokens:
- `username` – required
- `password` – required
- `email` – required
- `email_password` – to receive email code (you can use `--manual` mode to get code)
- `cookies` – can be any parsable format (string, json, base64 string, etc)
- `_` – skip column from parse

Tokens should be split by a delimiter, usually "`:`".

Example:

I have account files named `order-12345.txt` with format:
```text
username:password:email:email password:user_agent:cookies
```

The command to add accounts will be (the `user_agent` column is skipped with `_`):
```sh
twscrape add_accounts ./order-12345.txt username:password:email:email_password:_:cookies
```

### Login accounts

_Note:_ If you added accounts with cookies, login not required.

Run:

```sh
twscrape login_accounts
```

`twscrape` will start login flow for each new account. If X will ask to verify email and you provided `email_password` in `add_account`, then `twscrape` will try to receive verification code by IMAP protocol. After success login account cookies will be saved to db file for future use.

#### Manual email verification

In case your email provider not support IMAP protocol (ProtonMail, Tutanota, etc) or IMAP is disabled in settings, you can enter email verification code manually. To do this run login command with `--manual` flag.

Example:

```sh
twscrape login_accounts --manual
twscrape relogin user1 user2 --manual
twscrape relogin_failed --manual
```

### Get list of accounts and their statuses

```sh
twscrape accounts

# Output:
# username  logged_in  active  last_used            total_req  error_msg
# user1     True       True    2023-05-20 03:20:40  100        None
# user2     True       True    2023-05-20 03:25:45  120        None
# user3     False      False   None                 120        Login error
```

### Re-login accounts

It is possible to re-login specific accounts:

```sh
twscrape relogin user1 user2
```

Or retry login for all failed logins:

```sh
twscrape relogin_failed
```

### Use different accounts file

Useful if using a different set of accounts for different actions

```
twscrape --db test-accounts.db <command>
```

### Search commands

```sh
twscrape search "QUERY" --limit=20
twscrape tweet_details TWEET_ID
twscrape tweet_replies TWEET_ID --limit=20
twscrape retweeters TWEET_ID --limit=20
twscrape user_by_id USER_ID
twscrape user_by_login USERNAME
twscrape user_media USER_ID --limit=20
twscrape following USER_ID --limit=20
twscrape followers USER_ID --limit=20
twscrape verified_followers USER_ID --limit=20
twscrape subscriptions USER_ID --limit=20
twscrape user_tweets USER_ID --limit=20
twscrape user_tweets_and_replies USER_ID --limit=20
twscrape trends sport
```

The default output is in the console (stdout), one document per line. So it can be redirected to the file.

```sh
twscrape search "elon mask lang:es" --limit=20 > data.txt
```

By default, parsed data is returned. The original tweet responses can be retrieved with `--raw` flag.

```sh
twscrape search "elon mask lang:es" --limit=20 --raw
```

### About `limit` param

X API works through pagination, each API method can have different defaults for per page parameter (and this parameter can't be changed by caller). So `limit` param in `twscrape` is the desired number of objects (tweets or users, depending on the method). `twscrape` tries to return NO LESS objects than requested. If the X API returns less or more objects, `twscrape` will return whatever X gives.

## Proxy

There are few options to use proxies.

1. You can add proxy per account

```py
proxy = "http://login:pass@example.com:8080"
await api.pool.add_account("user4", "pass4", "u4@mail.com", "mail_pass4", proxy=proxy)
```

2. You can use global proxy for all accounts

```py
proxy = "http://login:pass@example.com:8080"
api = API(proxy=proxy)
doc = await api.user_by_login("elonmusk")
```

3. You can set a proxy with environment variable `TWS_PROXY`:

```sh
TWS_PROXY=socks5://user:pass@127.0.0.1:1080 twscrape user_by_login elonmusk
```

4. You can change proxy any time like:

```py
api.proxy = "socks5://user:pass@127.0.0.1:1080"
doc = await api.user_by_login("elonmusk")  # new proxy will be used
api.proxy = None
doc = await api.user_by_login("elonmusk")  # no proxy used
```

5. Proxy priorities

- `api.proxy` have top priority
- `env.proxy` will be used if `api.proxy` is None
- `acc.proxy` has the lowest priority

So if you want to use proxy PER ACCOUNT, do NOT override proxy with env variable or by passing proxy param to API.

_Note:_ If proxy not working, exception will be raised from API class.

## Environment Variables

- `TWS_PROXY` - global proxy for all accounts (e.g. `socks5://user:pass@127.0.0.1:1080`)
- `TWS_WAIT_EMAIL_CODE` - timeout for email verification code during login (default: `30`, in seconds)
- `TWS_RAISE_WHEN_NO_ACCOUNT` - raise `NoAccountError` exception when no available accounts, instead of waiting (default: `false`, values: `false`/`0`/`true`/`1`)

## Limitations

X/Twitter regularly [updates](https://x.com/elonmusk/status/1675187969420828672) their rate limits. Current basic behavior:
- Request limits reset every 15 minutes for each endpoint individually
- Each account has separate limits for different operations (search, profile views, etc.)

API data limitations:
- `user_tweets` & `user_tweets_and_replies` - can return ~3200 tweets maximum
- Rate limits may vary based on account age and status

## Articles
- [How to still scrape millions of tweets in 2023](https://medium.com/@vladkens/how-to-still-scrape-millions-of-tweets-in-2023-using-twscrape-97f5d3881434)
- [_(Add Article)_](https://github.com/alexander-schneider/twscrape/edit/main/readme.md)

## See also
- [twitter-advanced-search](https://github.com/igorbrigadir/twitter-advanced-search) – guide on search filters
- [TweeterPy](https://github.com/iSarabjitDhiman/TweeterPy) - Another X client
- [twitter-api-client](https://github.com/trevorhobenshield/twitter-api-client) – Implementation of Twitter's v1, v2, and GraphQL APIs
