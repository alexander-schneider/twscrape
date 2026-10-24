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
python -m pip install "twscrape @ git+https://github.com/alexander-schneider/twscrape.git@v0.20.1"
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
request retries and account rotation.

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

Proxy precedence is `API(proxy=...)`, then `TWS_PROXY`, then the account's saved
proxy. Setting `api.proxy = None` restores the environment/account fallback.
It does not force a direct connection.

```python
api = API("accounts.db", proxy="http://user:pass@127.0.0.1:8080")
```

To use account-specific proxies, leave both the API override and `TWS_PROXY`
unset. SOCKS proxies require the optional HTTPX SOCKS dependency:

```sh
python -m pip install "httpx[socks]"
```

## Environment variables

| Variable | Purpose |
| --- | --- |
| `TWS_PROXY` | Global proxy URL, between the API and account settings in priority. |
| `TWS_WAIT_EMAIL_CODE` | Email verification timeout in seconds. Default `30`. |
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
