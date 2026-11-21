# Changelog

## v0.22.5 - 2026-11-21

### Fixed

- A proxy curl-cffi takes from the environment (`https_proxy`, `HTTPS_PROXY`,
  `http_proxy`, `all_proxy` and their upper-case forms) counts as configured:
  transport errors then keep only their type, as for a configured proxy.
  Before, a malformed environment proxy reached the queue's retry warning with
  its credentials.
- The log sink twscrape installs on import (`twscrape.logger.add_default_sink`)
  runs without loguru's `backtrace` and `diagnose`, which print the values of a
  traceback's local variables, proxy arguments included. An application that
  adds its own loguru sink should pass `diagnose=False` too.

### Known limit

- httpcore logs connection details, including a proxy host, at DEBUG. With a
  malformed proxy whose password is parsed as the host name, that record shows
  it. Keep the `httpcore` logger at INFO or above in production.

## v0.22.4 - 2026-11-21

### Changed

- With a proxy configured, transport errors keep only their type. Every error the
  HTTP transport (httpx or curl-cffi) or the proxy setup raises becomes a new
  error of the same type whose message is `proxy request failed; details
  withheld because a proxy is configured (<type>)` or `proxy setup failed; …`;
  the original message, cause and context are dropped. Substring redaction is
  gone: no rule recognised every malformed proxy value (percent-encoded or
  full-width separators, `host:port:user:password`, e-mail user names), and
  libcurl and httpx quote the proxy URL with its credentials. Without a proxy,
  errors are unchanged.

### Fixed

- Every HTTP 5xx is classified by its status before the debug dump, the body or
  the headers are touched. A 503 whose `errors` field had the wrong shape (for
  example `{"errors": 1}`) took the 15-minute unknown-error lock, and debug mode
  failed on an undecodable body before the 5xx rule.

## v0.22.3 - 2026-11-21

A third review of the search refusal handling found three gaps.

### Fixed

- A proxy password with a full-width delimiter (for example U+FF1A, which
  becomes ":" under NFKC) made `urlsplit()` raise a `ValueError` that quoted
  the credentials before the sanitiser ran. Such a proxy now gets the generic
  `proxy URL rejected by httpx/curl; credentials removed` message.
- Every HTTP 5xx is classified as a transient `ServiceUnavailableError` before
  the body is decoded or the rate-limit headers are parsed. Before, a 503 with
  a body that is not UTF-8, or with a rate-limit header that is not a number,
  escaped that rule and took the 15-minute unknown-error lock.
- The signed control read before a search quarantine counts only with a
  positive integer user id; `rest_id` 0, negative or not a number is an
  inconclusive read (30-minute cooldown), not a confirmed refusal.

## v0.22.2 - 2026-11-21

Two more reviews of v0.22.1 found credentials still reachable through
exception chains, DEBUG logs and unparseable proxy URLs, and gaps in the
search refusal handling.

### Fixed

- No proxy credentials in exception chains. The clean transport error was
  raised inside the `except` block, so its `__context__` still held the
  original libcurl or httpx error with the proxy URL, which Sentry, loguru's
  `diagnose` and `exc_info` formatters print. Clean errors are now raised
  outside the block, and an error whose cause, context or group members carry
  credentials is replaced too.
- No credentials in aiosqlite DEBUG logs. aiosqlite logs every database call
  with its SQL parameters (proxy URLs, passwords, cookies). twscrape adds a
  filter to the `aiosqlite` logger that drops records below WARNING, also when
  an application sets that logger to DEBUG.
- No fragments of a proxy password. When a proxy URL does not parse cleanly
  (for example a raw `/` in the password, or `:` and `@` in it), httpx and curl
  print pieces of the password, such as `Invalid port: 'pa%40ss'`. If the
  proxy URL does not parse cleanly, or a userinfo fragment of 3 or more
  characters survives redaction, the message becomes `proxy URL rejected by
  httpx/curl; credentials removed`. The exception type is kept.
- Every HTTP 5xx is transient, whatever its body. A 503 with a plain-text or
  empty body (or a 5xx with an unknown GraphQL error) took the 15-minute
  account lock and retried on another account. It now raises
  `ServiceUnavailableError` on the temporary-failure path, for every queue.
- A control read that answers `{"data": null}`, `{"data": {"user": null}}`, a
  non-object or invalid JSON no longer raises `AttributeError` with no
  cooldown. It is inconclusive: the account cools down for 30 minutes and the
  request fails.
- Saving a loaded `Account` (for example after a login) keeps its search
  refusal streak; v0.22.1 reset it, so the graded lock started again at 30
  minutes.
- Lock times are compared by time, not text. `save` writes
  `2026-11-21T09:00:00+00:00` and `lock_until` writes `2026-11-21 14:00:00`;
  as text the first sorts later. The wait estimate took the 09:00 lock over the
  14:00 one, and an expired ISO lock kept the account out of `get_for_queue`
  and counted in `stats`.

### Changed

- `twscrape reset_locks` (`AccountsPool.reset_locks`) also ends every search
  refusal streak, so an account an operator puts back starts at the 30-minute
  step again.

## v0.22.1 - 2026-11-21

A review of v0.22.0 found that one short X-wide error could keep a whole pool
out of search for 6 hours, and that a malformed proxy could log its
credentials.

### Fixed

- Proxy credentials no longer reach logs or raised errors. libcurl puts the
  proxy URL into some errors (for example a malformed port), and the queue
  client logged that text at WARNING. Every transport error, for curl-cffi and
  for plain httpx, now has the userinfo of any URL and the literal proxy
  username and password replaced with `***`.
- Only an HTTP 200 SearchTimeline answer with `(-1) Internal server error` and
  no timeline data is a refused search. A 5xx with the same text takes the
  temporary-failure path again (move to a free account, release the accounts
  at the end), as before v0.22.0.
- Both refusal shapes (that 200 answer and the 404 after three fresh
  x-client-transaction-ids) go through the signed control read
  (UserByScreenName `X`). It counts only with user data
  (`data.user.result.rest_id`), not with any HTTP 200. Without it the account
  cools down for 30 minutes and the request fails: `NotFoundAfterSigningError`
  for the 404, `ServiceUnavailableError` for the 200 answer.
- `get_for_queue_or_wait` gives up at once when an account's global lock has
  ended but its queue lock still runs for hours. Per account the later of its
  queue lock and its global lock now counts, then the earliest account. Before,
  the expired global lock read as "available now" and the request waited out
  its full timeout.

### Changed

- The search refusal lock escalates per account instead of a flat 6 hours. The
  first refusal locks SearchTimeline for `TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS`
  (new, default 30 minutes); each consecutive refusal of the same account
  doubles it, capped at `TWS_SEARCH_REFUSAL_LOCK_SECONDS` (default 6 hours):
  30 min, 1 h, 2 h, 4 h, 6 h, 6 h. A 2-minute X-wide error thus costs each
  account 30 minutes, and a limited account reaches 6 hours after a few probes.
  The count is stored in `accounts.db`, in the account's `stats` JSON as
  `{"__search_refusals__": {"SearchTimeline": n}}`; the request counters and the
  `Account` model skip it. It goes back to 0 when the account completes a
  search with timeline data. Saving a loaded `Account` (for example after a
  login) also resets it.
- CI tests Python 3.14.

## v0.22.0 - 2026-11-21

X stopped serving search to every account of a production pool on 2026-11-21
(the first six on 2026-11-05). The accounts stayed limited, and the 5-minute
hold sent each of them about 400 more refused searches in two hours. These
changes make each account look like one browser on one connection and stop
asking X for search once it refused an account.

### Changed

- An account's own `proxy` now wins over the API `proxy` and `TWS_PROXY`, for
  API requests and for the x-client-transaction-id page fetch. Before, a
  shared `TWS_PROXY` overrode every account, so all accounts left through one
  gateway. Accounts without their own proxy still use the API proxy, then
  `TWS_PROXY`.
- SearchTimeline is sent as GET, like the x.com web client, instead of POST.
  Upstream declined the POST route as easier to detect
  ([#368](https://github.com/vladkens/twscrape/pull/368)).
- A refused search quarantines the account for search: HTTP 200 with
  `(-1) Internal server error` and no data, or HTTP 404 after three fresh
  x-client-transaction-ids. The account gets a SearchTimeline lock for
  `TWS_SEARCH_REFUSAL_LOCK_SECONDS` (default 6 hours) instead of 5 minutes and
  is not released when every account fails. The request moves to a free
  account; without one, or after three refused accounts, it raises
  `SearchRefusedError` (a `ServiceUnavailableError`). Each refused account is
  asked once per request. Other transient errors keep their behavior.
- `get_for_queue_or_wait` gives up at once when the earliest account comes back
  later than the wait could last and later than an in-use account's 15-minute
  placeholder, for example when every account is quarantined from search.
  Before, each request waited out its full timeout.

- The x-client-transaction-id animation key now matches Chromium: colour
  channels are clamped to 0..255, the easing is solved like Chromium's
  `gfx::CubicBezier`, matrix entries are rounded from their printed form, and
  the frame time is rounded to tens as X's script does. Before, 441 of 1000
  browser-recorded keys diverged (at least 4.9% of random keys printed a channel
  above 255), and X answers such ids with an empty 404. Ported from
  [Lqm1/x-client-transaction-id#26](https://github.com/Lqm1/x-client-transaction-id/pull/26);
  `tests/browser_capture/xtid` holds the Apache-2.0 vectors of
  [anatolykoptev/go-twitter](https://github.com/anatolykoptev/go-twitter).
- The signing indices are also found in X's hashed x-web build: static imports
  are followed from the page scripts (2 levels, 32 chunks) to the chunk loaded
  with `import(x).then(m => m.default())`. A chunk needs at least two
  `(a[N], 16)` indices to count, so an unrelated chunk with one match is not
  taken. Adapted from [vladkens/twscrape#380](https://github.com/vladkens/twscrape/pull/380).
- Before a search 404 starts the quarantine, one signed control read
  (UserByScreenName) checks that the account still reads. If it fails too, the
  account cools down for 30 minutes (past the 15-minute wait horizon) and the
  request fails as before. X does not check the transaction id on this read
  (measured 2026-11-21), so it does not detect a broken signature.
- SearchTimeline sends the web client's variables in its order
  (`withGrokTranslatedBio` true for Top and People only,
  `withQuickPromoteEligibilityTweetFields`), exactly the feature switches the
  web client sent on 2026-11-20 (fa0311/twitter-openapi#147) and no
  `fieldToggles`. Other operations keep `GQL_FEATURES`.

### Added

- Optional browser fingerprint: `API(impersonate="chrome")` or
  `TWS_IMPERSONATE=chrome` sends requests through curl-cffi with that browser's
  TLS and HTTP/2 fingerprint while twscrape keeps its httpx client. The browser
  profile sets the user agent and client hints; the stored account user agent
  is not sent. API calls (`/i/api/`, `/graphql/`, `/1.1/`) carry the web
  client's fetch headers (`accept: */*`, `sec-fetch-mode: cors`,
  `sec-fetch-dest: empty`, `sec-fetch-site`, `priority: u=1, i`, the search page
  as referer for SearchTimeline, `origin` on POST) instead of the profile's
  page-load headers. Selectively adapts the idea of upstream
  [#363](https://github.com/vladkens/twscrape/pull/363). New dependency:
  `curl-cffi>=0.16.3`.

## v0.21.3 - 2026-11-09

### Fixed

- Move a search to a free account after a temporary X failure and hold the
  failing account for search for five minutes. X answered SearchTimeline for
  some accounts with HTTP 200 and `(-1) Internal server error` while other
  accounts worked; the request retried the refused account until it failed.
  Without a free account, the request still retries the current one. If every
  account it tried fails, the failure is X-wide: the request releases those
  accounts and raises after the existing three requests. Other operations keep
  retrying the same account.
- Do not yield tweets that X only embeds as the quote of another result. A
  quoted tweet that has its own timeline entry is still yielded. Search,
  timelines and lists return fewer items: in the recorded search fixture 10
  instead of 11. Selectively adapts the parser part of upstream
  [#338](https://github.com/vladkens/twscrape/pull/338).

Changes: [PR #26](https://github.com/alexander-schneider/twscrape/pull/26) and
[PR #27](https://github.com/alexander-schneider/twscrape/pull/27).

## v0.21.2 - 2026-11-09

### Fixed

- Refresh SearchTimeline and TweetDetail operation IDs from upstream v0.21.0.
- Cool and rotate rate-limited accounts when response headers are missing or
  expired. Preserve usable data and keep accounts active after HTTP 429.
- Deactivate suspended accounts reported with code 64 and continue with the
  next account. Selectively adapts upstream
  [#364](https://github.com/vladkens/twscrape/pull/364).
- Honor explicit suspension and access-denied errors before quota headers.
  Preserve successful request counts atomically when deactivating an account.
- Retry transient GraphQL errors behind empty nested object wrappers instead
  of treating them as successful empty results. Persistent failures raise after
  the existing retry limit, preserving results already yielded.

Changes: [PR #24](https://github.com/alexander-schneider/twscrape/pull/24).

## v0.21.1 - 2026-10-28

### Fixed

- Propagate aborted requests and transaction-ID parsing failures through raw and
  parsed API methods instead of returning empty results. Export `AbortReqError`
  and `XClIdParseError`, and exit the CLI with code 1 on these failures while
  preserving earlier output. Selectively adapts request failure propagation from
  upstream [#347](https://github.com/vladkens/twscrape/pull/347).

## v0.21.0 - 2026-10-25

### Fixed

- Preserve complete user profiles when X repeats them as partial copies.
- Keep tweets with missing author profiles, including retweets and quotes,
  without discarding known handles or reply references. Use valid ID-based URLs
  when a handle is unavailable.
- Count unique parsed results for search limits and stop after three consecutive
  pages without new parsed results.
- Retry transient GraphQL errors whose direct data fields are empty.

### Maintenance

- Shorten the fork README and move local development, live checks and release
  instructions into a separate guide.
- Correct account, proxy and limit examples and ignore generated development
  files.

Changes: [PR #18](https://github.com/alexander-schneider/twscrape/pull/18).
Author-profile fixes adapt upstream [#344](https://github.com/vladkens/twscrape/pull/344)
and [#345](https://github.com/vladkens/twscrape/pull/345).

## v0.20.1 - 2026-09-01

### Added

- Add cookie-only account creation and session refresh through the API and CLI
  while preserving existing credentials, proxy settings, statistics, and locks.
- Add configurable, bounded account waiting with `wait_timeout` and
  `wait_interval`.

### Fixed

- Update existing X GraphQL operation IDs to upstream v0.20.1 and parse current
  user-profile fields, including responses without the legacy profile key.
- Stop repeated pagination cursors and pages without dropping a newly fetched
  page.
- Retry all `httpx` transport errors and LoadShed responses with bounded backoff
  before cooling or rotating accounts.
- Preserve usable GraphQL data alongside fully parsed warnings, while failing
  closed on malformed or unknown error payloads without trusted data.

## v0.19.9 - 2026-08-29

- Keep accounts active after ambiguous JSON HTTP 403 responses and cool only the
  affected queue before rotating. Explicit authentication and access-control
  errors still deactivate the account.

## v0.19.8 - 2026-07-29

### Fixed

- Update X GraphQL operation IDs to the current upstream v0.19.2 values.
- Support current `x-web`/Vite signing bundles and classify transaction-ID
  account and parser failures separately for safe account rotation.
- Require complete `auth_token` and `ct0` cookie sessions before activating an
  account.
- Avoid emitting nested retweet originals as duplicate top-level tweets while
  preserving originals that have explicit timeline entries.

### Maintenance

- Keep CI on the Ruff 0.15 rule defaults until the expanded Ruff 0.16 rules are
  adopted explicitly.

## v0.19.7 - 2026-07-19

### Fixed

- Treat X `DeadlineExceeded` responses as transient backend failures instead of
  cooling healthy accounts for 15 minutes.

## v0.19.6 - 2026-06-11

### Fixed

- Retry transient X API failures without cooling healthy accounts as unknown
  errors.
- Use account metadata and verified X assets when generating client transaction
  IDs.

## v0.19.5 - 2026-05-28

### Fixed

- Reconcile the fork with upstream v0.18.1 X compatibility fixes for current
  GraphQL payload shapes, including `legacy = null` tweets/users and moved user
  fields.
- Treat `Dependency: Unspecified` as a transient X backend failure.
- Continue SearchTimeline pagination past bounded empty pages while preserving
  repeated cursor/page stall protection.
- Harden current-shape user/tweet flattening against malformed nested fields.

## v0.19.4 - 2026-05-20

### Fixed

- Resolve X client transaction ID script URLs from numeric chunk name/hash maps
  when `ondemand.s` is no longer exposed as a direct mapping.

## v0.19.3 - 2026-04-18

### Fixed

- Parse X `poll_choice_images` card names as `PollCard` instead of logging them
  as unknown card types.
- Treat known transient X GraphQL errors (`ServiceUnavailable`, `Internal server
  error`, and `Timeout: Unspecified`) as typed `ServiceUnavailableError`
  failures without cooling healthy accounts as unknown API failures.
