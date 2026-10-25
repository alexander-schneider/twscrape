# Develop and verify the fork

## Install a local checkout

```sh
git clone https://github.com/alexander-schneider/twscrape.git
cd twscrape
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

The project supports Python 3.10 and newer. CI tests Python 3.10 through 3.13.
Keep the virtual environment active for the commands below.

## Offline checks

```sh
make lint
make test
```

`make lint` sorts imports, formats code, runs Ruff and runs Pyright. `make test`
uses mocked responses and excludes tests marked `live`. Neither command needs an
X account. Target one area during development:

```sh
python -m pytest tests/test_parser.py -q
python -m pytest tests/test_api.py -q
python -m pytest tests/test_pool.py tests/test_queue_client.py -q
```

Fixtures live in `tests/mocked-data/`. Keep regression fixtures small and tests
offline. Preserve public async methods, model fields, HTTPX raw response types,
global search de-duplication, account cooldowns and the SQLite compatibility
fallback. See [AGENTS.md](../AGENTS.md) for the repository map and change rules.

## Adanos compatibility checks

The normal offline suite includes end-to-end API pipeline tests. They replace
external HTTP responses and signing assets while running the real client,
account pool, SQLite updates and parsers. Run only this group with:

```sh
make test-e2e
```

To exercise the actual private Adanos wrappers, supply a local consumer checkout
and install its lightweight configuration dependency:

```sh
python -m pip install pydantic-settings
ADANOS_SOURCE_ROOT=/path/to/api.adanos.org make test-adanos
```

This target requires the real source. It checks imports, account and proxy
behavior, UTC search filtering, normalization, pagination, direct replies,
budgets, retries, partial results and logging. It does not start PostgreSQL,
inference or a complete worker. The private consumer workflow runs this same
target against public twscrape main and records both source commits and Adanos'
declared dependency pin. Testing main does not update that pin or Production.

Public PRs run without private source or live credentials. Their JUnit reports
contain only offline test data. The private wrapper tests run in the private
repository. Keep those results separate from the live library check.

### Current X acceptance in Actions

The [Live X compatibility workflow](../.github/workflows/live-x.yml) runs daily
and through manual dispatch on reviewed `main`. Its `x-live` GitHub environment
must allow only the `main` branch and contain these secrets:

- `TWS_LIVE_USERNAME` identifies a dedicated test account.
- `TWS_LIVE_COOKIES` contains nonempty `auth_token` and `ct0` cookies.
- `TWS_LIVE_PROXY` is the required proxy URL.

The workflow uses real signing and requires both positive search results and
a direct reply. It bounds requests and runtime, serializes the test account,
and fails when configuration is missing or positive data cannot be established.
It performs no password login or session refresh.

Only the sanitized JSON report is uploaded. Account databases, raw logs,
response bodies and parser dumps remain excluded. An empty result, HTTP 200,
skipped job or offline replay does not establish current X acceptance.

Run the same bounded probe locally with test credentials in the environment:

```sh
python scripts/check_adanos_live.py --report /tmp/twscrape-live.json
```

`ADANOS_SOURCE_ROOT` optionally enables actual-wrapper execution locally.
Set `TWS_REQUIRE_ADANOS=1` to require that mode. The public workflow has no
private checkout, so its report always identifies library-only live evidence.

## Live checks

These commands contact X and require your authorized account. They are separate
from the offline suite. `make check` runs lint, offline tests **and** the live
test. Do not run it in an offline debugging loop.

### Share a local test session

Seed a local session once with hidden cookie prompts:

```sh
export TWS_LIVE_PROXY="http://proxy.example:8080"
make test-live-seed-prompt
make test-live
```

The default session database is `~/.local/share/twscrape/live-check.db`, outside
the worktree. This allows separate worktrees to reuse the same test session.
`make test-live-seed-prompt` resets that test database before preparing it. Use a
dedicated test database, never a worker's account database.

Override the path for the Make targets with `LIVE_DB`:

```sh
make test-live LIVE_DB=/path/to/test-accounts.db
```

The live test searches `NVDA` within the current UTC day, then requires a direct
reply from a searched parent. `TWS_LIVE_PROXY` is required even when the saved
database contains account proxies. To use another ticker, run the CLI probe
with `--ticker SYMBOL`.

`TWS_LIVE_USERNAME` and `TWS_LIVE_COOKIES` provide a disposable cookie-session
database instead of the saved test database. Otherwise the probe uses a
read-only SQLite backup and preserves existing cooldowns. Prefer the hidden
prompt setup to entering cookies in shell history.

### Manual smoke test

```sh
make smoke-prompt
make smoke
make smoke-stock TICKER=NVDA
make smoke-stock TICKER=TSLA HOURS=12 MIN_FAVES=5 LIMIT=10
```

The manual smoke script uses `.local/smoke.db` by default. It checks a user lookup
and a small search. `make smoke-prompt`, `make smoke-reset` and
`make smoke-stock-prompt` delete that smoke database before preparing the account.
`make smoke` and `make smoke-stock` reuse it.

Inspect all script options without contacting X:

```sh
python examples/local_smoke_test.py --help
```

For a custom probe with a saved session:

```sh
python examples/local_smoke_test.py \
  --probe-user OpenAI \
  --query 'from:OpenAI' \
  --limit 3
```

Pass `--proxy` or use `TWS_PROXY` if required. Password-login debugging uses
`--login --manual-login` and prompts for credentials and verification codes.

### Refresh live fixtures

`make update-mocks` contacts X, requires `jq` and working accounts, and replaces
the saved `raw_*.json` fixtures. Run it only for deliberate fixture maintenance.
Inspect the resulting diff before committing.

`python _get_gql_ops.py` is also a live maintenance command. It reads X assets and
prints candidate operation IDs. Review its output before updating `twscrape/api.py`.

## Fork release checklist

A PR does not publish a release. Before tagging a fork release:

1. Update `pyproject.toml` and `CHANGELOG.md` for the release.
2. Run the offline checks and a separately authorized live check.
3. Build a wheel and inspect its package metadata.
4. Confirm the release tag matches the package version.

```sh
python -m build --wheel
python -c "import glob, zipfile; p = glob.glob('dist/twscrape-*.whl')[-1]; z = zipfile.ZipFile(p); print(z.read(next(n for n in z.namelist() if n.endswith('/METADATA'))).decode())"
```

Check the intended wheel if `dist/` contains multiple builds. Downstream projects
pin a fork Git tag, so tag and wheel metadata must agree. Publishing a release or
tag is a separate maintainer action.
