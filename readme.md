# twscrape

Async Python library and CLI for X/Twitter search and GraphQL endpoints, with
[SNScrape](https://github.com/JustAnotherArchivist/snscrape)-style models.

This is the Adanos-maintained fork of
[`vladkens/twscrape`](https://github.com/vladkens/twscrape). Fixes for Adanos land
here first. Open [issues](https://github.com/alexander-schneider/twscrape/issues)
and pull requests in this repository.

## Install the fork

Python 3.10 or newer is required. Install the released fork from Git:

```sh
python -m pip install "twscrape @ git+https://github.com/alexander-schneider/twscrape.git@v0.21.0"
```

`pip install twscrape` installs the upstream PyPI package. To develop this fork,
use the [development guide](docs/development.md).

## Start with cookies

Use an X account you are authorized to access. Copy its `auth_token` and `ct0`
cookies from your authenticated browser session. Both must have nonempty values.

```sh
twscrape add_cookie my_account
twscrape accounts
twscrape search 'from:xdevelopers lang:en' --limit 20
```

`add_cookie` reads cookies from a hidden prompt or stdin. It accepts a cookie
header or a JSON cookie export. Cookies are stored in the local SQLite database,
so protect that file as you would the session itself.

Run `add_cookie` again to refresh the session. Existing credentials, proxies,
statistics and queue locks remain in place. A complete cookie session activates
the account immediately. Password login is not needed for cookie-only accounts.

The default database is `accounts.db` in the current directory. Select another
database before the command:

```sh
twscrape --db /path/to/accounts.db search 'from:xdevelopers' --limit 20
```

## Python API

After adding the account with the CLI, use the same database from Python:

```python
import asyncio

from twscrape import API, gather


async def main():
    api = API("accounts.db")
    tweets = await gather(api.search("$NVDA lang:en", limit=20))
    for tweet in tweets:
        print(tweet.id, tweet.user.username, tweet.rawContent)


asyncio.run(main())
```

Inside your async function, you can also create or refresh a cookie-only account:

```python
from getpass import getpass

await api.pool.add_account_cookies("my_account", getpass("X cookie export: "))
```

Methods return parsed models. Their `_raw` variants return `httpx.Response`
objects, or async generators of responses for paginated methods. Use `gather`
to collect a generator, or consume it with `async for`.

| Task | Python method |
| --- | --- |
| Search tweets or people | `search`, `search_user` |
| Read a tweet or its replies | `tweet_details`, `tweet_replies` |
| Read users who retweeted | `retweeters` |
| Look up a user | `user_by_login`, `user_by_id` |
| Read account relationships | `followers`, `following`, `verified_followers`, `subscriptions` |
| Read user timelines | `user_tweets`, `user_tweets_and_replies`, `user_media` |
| Read a list | `list_timeline` |
| Read trends | `trends`, `search_trend` |
| Read saved tweets | `bookmarks` |

Other calls inside an async function:

```python
user = await api.user_by_login("xdevelopers")
tweet = await api.tweet_details(1649191520250245121)
replies = await gather(api.tweet_replies(1649191520250245121, limit=20))
top = await gather(api.search("python", limit=20, kv={"product": "Top"}))
```

Models keep SNScrape-style fields such as `rawContent`, `followersCount`,
`retweetedTweet` and `quotedTweet`. Call `model.dict()` or `model.json()` to
serialize one.

### Limits and missing profiles

`search` and `search_user` emit at most `limit` unique models. They may return
fewer when X exhausts its results or pagination stops making progress. `limit=-1`
removes the requested count limit. Repeated cursors/pages and consecutive pages
without new parsed results still stop the crawl.

Raw methods return whole server pages. Their limits count raw entries and may
include a final page larger than the requested count. Other paginated methods
also retain their page-based limit behavior.

X sometimes returns a complete tweet without its author's profile. The parser
keeps the tweet and the known author ID. It preserves known handles and names.
Unavailable profile fields use the existing model defaults, so zero counts and
empty strings can mean unknown metadata. URLs fall back to the tweet or user ID
when the handle is missing. Do not infer that a missing handle passed an
author-based filter.

### Account waiting and errors

```python
api = API("accounts.db", wait_timeout=30, wait_interval=1, raise_when_no_account=True)
```

`wait_timeout=None` keeps the default unbounded wait for locked accounts.
`wait_timeout=0` returns immediately when none is available.
`raise_when_no_account=True` raises `NoAccountError` instead of ending the request
when the pool has no usable account.

The fork exposes `ServiceUnavailableError` for known temporary X failures,
`ApiFeatureUpdateRequiredError` for missing required GraphQL features, and
`ParseDriftError` when too many items in one response fail parsing. Callers own
their retry and partial-result policy. The library preserves its own bounded
request retries and account rotation. X refuses search for some accounts only,
so a temporary search failure moves the request to a free account and holds the
failing account for search for five minutes. Without a free account, the request
retries the current one. If every account it tried fails, the request releases
them and raises. Other operations retry the same account.

A refused search is an HTTP 200 SearchTimeline answer with `(-1) Internal server
error` and no timeline data, or HTTP 404 after three fresh
x-client-transaction-ids (one on a paced queue, see below). Before it counts, one signed control read
(UserByScreenName `X`) must return user data (`data.user.result.rest_id`). Then
the account is locked for search: 30 minutes for its first refusal, doubled for
each consecutive refusal up to 6 hours (see the `TWS_SEARCH_REFUSAL_*` variables).
The count of consecutive refusals is stored per account in `accounts.db`. It
survives saving the account (for example after a login) and goes back to 0 when
the account completes a search with timeline data or when `twscrape reset_locks`
(`AccountsPool.reset_locks`) lifts the locks. If the control read returns no
user data, or an answer it cannot read, the account cools down for 30 minutes
and the request fails. Every HTTP 5xx, whatever its body, is never a refusal and
never an account lock; it takes the temporary-failure path above. The request moves to a free account after a refusal and raises
`SearchRefusedError` (a `ServiceUnavailableError`) without one.

`AbortReqError` and `XClIdParseError` are also exported from `twscrape`. Requests
raise these errors when aborted or when transaction-ID parsing fails, instead of
returning an empty result. Both raw and parsed methods propagate the original
error. Paginated methods keep earlier yields and raise on the failed page.
The CLI exits with code 1, retaining any results already printed. Genuine empty
responses and not-found lookups still return no items or `None`.

### Close an interrupted generator

Use `aclosing` when breaking out early so the account lock is released promptly:

```python
from contextlib import aclosing

async with aclosing(api.search("python")) as tweets:
    async for tweet in tweets:
        print(tweet.id)
        break
```

## CLI

Run `twscrape` to list commands, or request help for one command:

```sh
twscrape search --help
twscrape search '$NVDA lang:en' --limit 20
twscrape user_by_login xdevelopers
twscrape tweet_details 1649191520250245121
twscrape search 'python lang:en' --limit 20 --raw
```

Output is one JSON document per line on stdout. Diagnostics go to stderr. Use
`--raw` for the original response payload and `--debug` before the command for
verbose logs.

### Password accounts

Cookie auth avoids the changing password-login challenge flow. Password login
remains available when needed:

```sh
twscrape add_accounts accounts.txt username:password:email:email_password
twscrape login_accounts
twscrape relogin my_account
twscrape relogin_failed
```

The import format must include all four columns. Additional supported columns
are `mfa_code`, `user_agent`, `proxy` and `cookies`. Use `_` for an ignored column.
The file delimiter must match the delimiter in the format argument. When using
`login_accounts --manual`, the `email_password` column is still required by the
importer but can contain a placeholder. Protect account-import files too.

## Proxies

Proxy precedence is the account's saved proxy, then `API(proxy=...)`, then
`TWS_PROXY`. Give each account its own static proxy so it always leaves from one
address; the API proxy and `TWS_PROXY` are the fallback for accounts without
one. No proxy at all means a direct connection.

```python
api = API("accounts.db", proxy="http://user:pass@127.0.0.1:8080")
```

SOCKS proxies require the optional HTTPX SOCKS dependency:

```sh
python -m pip install "httpx[socks]"
```

## Account pacing, daily caps and warm-up

X suspends fresh accounts that burst (vladkens/twscrape#384: 48 s after a
3,000-tweet first run) and limits accounts that search at a steady high rate.
Three optional budgets keep each account slow. All are off when unset.

```sh
export TWS_ACCOUNT_PACING="SearchTimeline=60,UserTweets=15"  # seconds between two requests of one account
export TWS_ACCOUNT_DAILY_CAPS="SearchTimeline=400"           # requests per account and UTC day
export TWS_WARMUP_DAYS=14                                   # new accounts ramp up over 14 days
export TWS_WARMUP_START_FRACTION=0.1                        # ...starting at 10 % of the budget
```

- **Pacing.** The interval is a minimum between two sends of one account on a
  paced queue, stretched by a random factor in 1.0..1.4. Every send waits for
  the account's last send plus that pace, also retries within one request
  (transport errors); when the account goes back to the pool its lock for the
  queue lasts at least that long, so the minimum also holds across requests and
  processes. A longer lock (rate limit, refusal, cooldown) wins. The account
  goes back to the pool after each request, so the next page of a search goes to
  the next free account. A paced queue signs a request once: a 404 is not
  retried with fresh x-client-transaction-ids (three attempts would take three
  intervals); a search 404 still goes to the control read and the refusal path.
  Waiting requests wait for a paced account as for one in use, while the wait
  is under about 16 minutes; with `raise_when_no_account` and no
  `wait_timeout`, they raise at once, as before.
- **Daily caps.** Every request sent on a capped queue is counted before it is
  sent (failed ones too, as X counts them), per UTC day, in the account's stats
  in `accounts.db`. The request that reaches the cap is sent; then the account's
  queue is locked until the next UTC midnight. When no account is free and at
  least one active account is held on that queue by its cap,
  `raise_when_no_account` raises `BudgetExhaustedError` (a `NoAccountError`):
  the pool is out of capacity for today, even if X refuses the other accounts,
  because the capped one could still search. Only a pool without a capped
  account (every account refused, cooling down or rate limited) raises the plain
  `NoAccountError`, since that may be an outage.
- **Warm-up.** An account's age is counted from its first request under a
  budget (stored once as `__first_used__`) to the start of the current UTC day,
  so its cap and pace stay the same all day and step at midnight. Its fraction is
  `start + (1 - start) x min(1, age / days)`; the interval is divided by it and
  the cap multiplied by it (floor, at least 1). With the example above, a new
  account waits at least 600 s between searches and sends at most 40 on its
  first day.

`API.search` and `API.search_raw` take `max_pages` to cap the requests of one
call: every page request counts, also the cursor-only pages followed on their
own. While search is paced, `max_pages=1` keeps a search to one request:

```python
tweets = [t async for t in api.search("$NVDA lang:en", max_pages=1)]
```

Accounts added before v0.23.0 also start their warm-up clock on their first
request under v0.23.0, so set `TWS_WARMUP_DAYS` only when every existing account
should ramp up again. `twscrape reset_locks` lifts locks, including a cap lock,
but keeps the daily counts and `__first_used__`, so the next request of a capped
account locks it again; the cap ends only with the UTC day. With a budget set,
`twscrape accounts` shows each account's count, effective cap and interval.
Invalid values raise a `ValueError` naming the variable when the `API` or
`AccountsPool` is created.

### Keeping search accounts for search

```sh
export TWS_PREFER_SEARCH_LOCKED_QUEUES="TweetDetail"  # thread reads go to accounts out of search first
```

A listed queue takes the free accounts held longest on `SearchTimeline` first,
grouped by the order of magnitude of the remaining lock: accounts out of search
for years (a long refusal quarantine or an operator's lock), then accounts held
for hours (a cap, a rate limit), then accounts paced for minutes, then accounts
free to search, and accounts without any search lock last. Within a group the
pool's own order decides (username, or random for a pool set so), so the
accounts out of search share the queue instead of the one disabled last taking
it all. Accounts that are still warming up then spend their requests on search.
It is a preference only: when no such account is free, an account free to search
takes the queue at once. `SearchTimeline` itself cannot be listed, and unlisted
queues keep their order.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `TWS_PROXY` | Fallback proxy URL for accounts without their own proxy and without an API proxy. |
| `TWS_IMPERSONATE` | Browser profile for curl-cffi, e.g. `chrome`. Sends requests with that browser's TLS and HTTP/2 fingerprint and user agent. Unset keeps plain HTTPX. Same as `API(impersonate=...)`. |
| `TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS` | How long an account stays out of search after its first refused search. Each consecutive refusal of the same account doubles the lock, up to `TWS_SEARCH_REFUSAL_LOCK_SECONDS`. Default `1800` (30 minutes). |
| `TWS_SEARCH_REFUSAL_LOCK_SECONDS` | The longest search lock after consecutive refused searches of one account. Default `21600` (6 hours). With both defaults the locks are 30 min, 1 h, 2 h, 4 h, 6 h, 6 h... |
| `TWS_ACCOUNT_PACING` | Minimum seconds between two requests of one account per queue, e.g. `SearchTimeline=60,UserTweets=15`, stretched by a random 1.0..1.4. Unset: no pacing. |
| `TWS_ACCOUNT_DAILY_CAPS` | Requests per account, queue and UTC day, e.g. `SearchTimeline=400`. At the cap the queue is locked until UTC midnight. Unset: no caps. |
| `TWS_WARMUP_DAYS` | Days over which a new account ramps up from `TWS_WARMUP_START_FRACTION` to the full pacing and caps. Default `0` (off). |
| `TWS_WARMUP_START_FRACTION` | Share of the budget on an account's first day, in (0, 1]. Default `0.1`. |
| `TWS_PREFER_SEARCH_LOCKED_QUEUES` | Comma-separated queues, e.g. `TweetDetail`, that go first to the free accounts held longest on `SearchTimeline` (by order of magnitude of the remaining lock). A preference only. Unset: the pool's own order. |
| `TWS_WAIT_EMAIL_CODE` | Email verification timeout in seconds. Default `30`. |
| `TWS_DB_EXISTING_ONLY` | `1` opens an existing accounts database of the latest migration only and raises `sqlite3.OperationalError` for a missing, empty or older one instead of creating or migrating it. Default `0`. |
| `TWS_RAISE_WHEN_NO_ACCOUNT` | Raise when no account is available. Accepts `true`/`1` or `false`/`0`. Default `false`. |

X controls rate limits, account challenges, available result windows and response
schemas. Client-side retries and local tests cannot guarantee live availability.
Use the [live checks](docs/development.md#live-checks) separately from offline
development.

## Development and releases

See the [development guide](docs/development.md) for offline tests, real-account
smoke tests, fixture maintenance and the fork release checklist.
See [CHANGELOG.md](CHANGELOG.md) for fork changes and [LICENSE](LICENSE) for the
license. The original project and its authors remain credited in the package
metadata. Historical upstream [articles](https://medium.com/@vladkens/how-to-still-scrape-millions-of-tweets-in-2023-using-twscrape-97f5d3881434)
and [search-filter references](https://github.com/igorbrigadir/twitter-advanced-search)
provide background.

![CLI usage example](.github/example.png)
