# Repository Guide

## Purpose

This repository is the standalone home of the `twscrape` dependency used by `api.adanos.org`.
It started from `vladkens/twscrape`, but fixes should now land here first. Treat this repo as the source of truth, even if some metadata and documentation still point to the old upstream.

## Quick Start

```bash
pip install -e .[dev]
make lint
make test
```

Useful targeted commands:

```bash
python -m pytest tests/test_api.py -q
python -m pytest tests/test_parser.py -q
python -m pytest tests/test_pool.py tests/test_queue_client.py -q
make check
```

Notes:

- `make lint` runs import sorting, formatting, Ruff, and Pyright.
- `make test` is fully mocked and should stay fast and deterministic.
- `make update-mocks` is a live-network maintenance task. It requires working X accounts, a usable `twscrape` CLI setup, and `jq`. Do not use it in normal CI/debug loops.

## Repo Shape

- `twscrape/api.py`
  Public async API. Holds GraphQL operation IDs, feature flags, pagination, de-duplication, and search stall protection.
- `twscrape/queue_client.py`
  Request lifecycle around `httpx`. Handles account checkout, x-client-transaction-id generation, rate limits, retries, session expiry, bans, and account rotation.
- `twscrape/accounts_pool.py`
  SQLite-backed account pool. Owns account persistence, queue locks, active/inactive state, relogin flows, and high-level pool stats.
- `twscrape/db.py`
  SQLite migrations and retry-on-lock behavior. This is the concurrency safety layer for local DB access.
- `twscrape/account.py`
  Account model plus `httpx.AsyncClient` construction, including cookies, auth headers, CSRF token wiring, and proxy selection.
- `twscrape/login.py`, `twscrape/imap.py`
  X login flow, email verification, IMAP integration, and MFA handling.
- `twscrape/models.py`
  SNScrape-shaped dataclasses and parsers. Most breakages from X response drift will land here.
- `twscrape/cli.py`
  Thin CLI over the async API and account-pool operations.
- `tests/mocked-data/`
  Saved API fixtures for parser and pagination regressions.

## Downstream-Sensitive Interfaces

Be careful with these because `api.adanos.org` or other consumers are likely to depend on them:

- `twscrape.__init__` exports: `API`, `AccountsPool`, `Account`, `NoAccountError`, `gather`, and model classes.
- `API` method names and return contracts.
  `*_raw` methods return `httpx.Response` objects or async generators of responses.
  Non-raw methods return parsed models or async generators of models.
- Model field names and `dict()` / `json()` output.
  The project intentionally mirrors SNScrape-style names like `rawContent`, `followersCount`, `retweetedTweet`, etc.
- CLI command names and default DB behavior.

Do not rename public methods, model fields, or CLI commands casually.

## How Fixes Usually Map

- X response shape changed:
  Start in `twscrape/models.py`, then add or update regression coverage in `tests/test_parser.py` and `tests/mocked-data/`.
- GraphQL operation changed or new feature toggle became required:
  Update `twscrape/api.py`.
- Search pagination stalls or repeated pages:
  Inspect `API._gql_items`, `API._is_stalled_search_page`, and `API._iter_unique`.
- Rate-limit, 403, 404, or account switching bugs:
  Inspect `twscrape/queue_client.py` first, then the relevant pool methods in `twscrape/accounts_pool.py`.
- Locking or account availability bugs:
  Inspect `get_for_queue`, `get_for_queue_or_wait`, `_get_and_lock`, `lock_until`, `unlock`, and `mark_inactive`.
- Login or cookie/session issues:
  Inspect `twscrape/login.py` and `twscrape/account.py`.

## Change Rules

- Keep the async-first design. Do not add sync wrappers inside the core library.
- Preserve the SQLite compatibility fallback in `AccountsPool._get_and_lock` for runtimes without `RETURNING`.
- If you touch DB schema or account lock logic, expect subtle regressions. Add or update tests.
- Prefer parser-level and queue-level fixes over broad behavioral rewrites.
- Keep tests offline by default. If a change needs live X calls, isolate that from the regular test suite.
- Do not remove search de-duplication or repeated-page stopping without replacing them with equivalent protection.

## Testing Expectations

After a normal code change:

1. Run targeted tests for the touched area.
2. Run `make lint`.
3. Run `make test`.

Additional guidance:

- Parser changes should at least run `tests/test_parser.py`.
- Pagination/API changes should at least run `tests/test_api.py`.
- Pool, locking, or retry changes should at least run `tests/test_pool.py` and `tests/test_queue_client.py`.
- If you change migration logic, verify tests on a clean temp DB and an already-initialized DB path.

## Release and Metadata Notes

- `origin` already points to `alexander-schneider/twscrape`.
- `pyproject.toml` and parts of `readme.md` still reference the old upstream repository. Verify whether a future fix should also update docs/metadata.
- CI lives in `.github/workflows/ci.yml` and currently tests Python 3.10 through 3.13.

## Practical Defaults For Future Agents

- Read the tests before changing parser or queue behavior.
- Favor small, regression-driven patches.
- If X breaks something unexpectedly, capture the smallest failing fixture you can and add a focused test before broad refactoring.
