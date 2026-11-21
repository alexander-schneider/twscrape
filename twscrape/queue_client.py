import asyncio
import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from httpx import AsyncClient, Response

from .account import Account, resolve_proxy
from .accounts_pool import GLOBAL_LOCK_QUEUE, AccountsPool, has_required_cookies
from .logger import LogOnce, logger
from .utils import utc
from .xclid import XClIdAccountError, XClIdGen, XClIdParseError

ReqParams = dict[str, str | int] | None
TMP_TS = utc.now().isoformat().split(".")[0].replace("T", "_").replace(":", "-")[0:16]
LOADSHED_COOLDOWN_SECONDS = 120
LOADSHED_RETRY_LIMIT = 3
TRANSPORT_ERROR_COOLDOWN_SECONDS = 60
TRANSPORT_ERROR_RETRY_LIMIT = 3
TRANSIENT_API_ERROR_RETRY_LIMIT = 3
TRANSIENT_API_ERROR_COOLDOWN_SECONDS = 60 * 5
UNKNOWN_API_ERROR_COOLDOWN_SECONDS = 60 * 15
SEARCH_QUEUE = "SearchTimeline"
# A public account read for the signed control request before a search quarantine.
CONTROL_SCREEN_NAME = "X"
# A search 404 with a failing control read (bad signature, account out everywhere). Longer than the 15-minute
# in-use horizon of AccountsPool.get_for_queue_or_wait, so other searches give up at once instead of waiting.
SIGNING_FAILURE_COOLDOWN_SECONDS = 30 * 60
DEFAULT_SEARCH_REFUSAL_FIRST_LOCK_SECONDS = 30 * 60
DEFAULT_SEARCH_REFUSAL_LOCK_SECONDS = 6 * 60 * 60


def _env_seconds(name: str, default: int) -> int:
    try:
        return max(60, int(os.getenv(name, default)))
    except ValueError:
        return default


def search_refusal_lock_seconds(refusals: int = 1) -> int:
    """How long an account X refused search for stays out of search after its `refusals`-th consecutive refusal.

    TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS (default 30 minutes) for the first refusal, doubled for each further
    one, capped at TWS_SEARCH_REFUSAL_LOCK_SECONDS (default 6 hours): 30 min, 1 h, 2 h, 4 h, 6 h, 6 h...
    """
    cap = _env_seconds("TWS_SEARCH_REFUSAL_LOCK_SECONDS", DEFAULT_SEARCH_REFUSAL_LOCK_SECONDS)
    first = _env_seconds(
        "TWS_SEARCH_REFUSAL_FIRST_LOCK_SECONDS", DEFAULT_SEARCH_REFUSAL_FIRST_LOCK_SECONDS
    )
    doublings = min(max(refusals, 1) - 1, 32)  # 2**32 times a minute is past any cap
    return min(first * 2**doublings, cap)


class HandledError(Exception): ...


class RetryReqError(Exception): ...


class AbortReqError(Exception): ...


class ApiFeatureUpdateRequiredError(Exception): ...


class UnexpectedApiError(Exception): ...


class ServiceUnavailableError(UnexpectedApiError): ...


class SearchRefusedError(ServiceUnavailableError):
    """X refused SearchTimeline for every account tried; each one is quarantined (vladkens/twscrape#367)."""


class NotFoundAfterSigningError(AbortReqError):
    """HTTP 404 again after fresh x-client-transaction-ids."""


TRANSIENT_API_ERROR_FRAGMENTS = (
    "(-1) ServiceUnavailable",
    "(-1) Internal server error",
    "(-1) Dependency: Unspecified",
    "(-1) DeadlineExceeded",
    "(29) Timeout: Unspecified",
)


def is_html_edge_block(rep: Response, res: Any) -> bool:
    if rep.status_code != 403:
        return False

    content_type = rep.headers.get("content-type", "").lower()
    raw = res.get("_raw", "") if isinstance(res, dict) else ""
    if not isinstance(raw, str):
        return False

    raw_lower = raw.lower()
    return "text/html" in content_type and (
        "cloudflare" in raw_lower or "attention required" in raw_lower or "<html" in raw_lower
    )


def is_transient_api_error(err_msg: str) -> bool:
    return any(fragment in err_msg for fragment in TRANSIENT_API_ERROR_FRAGMENTS)


def has_data(rep: Response, res: Any) -> bool:
    if rep.status_code != 200 or not isinstance(res, dict):
        return False

    data = res.get("data")
    if not isinstance(data, dict):
        return False

    def present(value):
        if isinstance(value, dict):
            return any(present(item) for item in value.values())
        return value is not None

    return any(value not in (None, {}, []) and present(value) for value in data.values())


def _int_header(rep: Response, name: str) -> int:
    """An integer response header, or -1 when it is missing or not a number."""
    try:
        return int(rep.headers.get(name, -1))
    except (TypeError, ValueError):
        return -1


def _outage_detail(rep: Response) -> str:
    """The GraphQL errors of a 5xx answer when it has readable ones, otherwise only its status."""
    try:
        payload = rep.json()
    except ValueError:
        return f"HTTP {rep.status_code}"
    items = payload.get("errors") if isinstance(payload, dict) else None
    messages = [
        f"({item.get('code', -1)}) {item['message']}"
        for item in items or ()
        if isinstance(item, dict) and isinstance(item.get("message"), str)
    ]
    return "; ".join(dict.fromkeys(messages)) or f"HTTP {rep.status_code}"


def has_user_data(rep: Response) -> bool:
    """A UserByScreenName answer with a user id (data.user.result.rest_id), not only HTTP 200."""
    if rep.status_code != 200:
        return False
    try:
        res = rep.json()
    except ValueError:
        return False
    # Every level may be null, a list or missing; only a dict at each level leads to a user id.
    data = res.get("data") if isinstance(res, dict) else None
    user = data.get("user") if isinstance(data, dict) else None
    result = user.get("result") if isinstance(user, dict) else None
    rest_id = result.get("rest_id") if isinstance(result, dict) else None
    if isinstance(rest_id, bool) or not isinstance(rest_id, (str, int)):
        return False
    # A user id is a positive integer, also when X sends it as a string.
    return str(rest_id).isdigit() and int(rest_id) > 0


def has_error(errors: list[str], prefix: str) -> bool:
    return any(error.startswith(prefix) for error in errors)


class XClIdGenStore:
    items: dict[str, XClIdGen] = {}  # username -> XClIdGen

    @classmethod
    async def get(
        cls,
        acc: Account,
        proxy: str | None = None,
        fresh=False,
        impersonate: str | None = None,
    ) -> XClIdGen:
        username = acc.username
        if username in cls.items and not fresh:
            return cls.items[username]

        selected_proxy = resolve_proxy(acc.proxy, proxy)

        tries = 0
        while tries < 3:
            try:
                clid_gen = await XClIdGen.create(
                    proxy=selected_proxy,
                    user_agent=acc.user_agent,
                    cookies=acc.cookies,
                    impersonate=impersonate,
                )
                cls.items[username] = clid_gen
                return clid_gen
            except httpx.HTTPStatusError:
                tries += 1
                await asyncio.sleep(1)

        raise AbortReqError(
            "Failed to create XClIdGen. See: https://github.com/alexander-schneider/twscrape/issues"
        )


class Ctx:
    def __init__(
        self,
        acc: Account,
        clt: AsyncClient,
        proxy: str | None = None,
        impersonate: str | None = None,
    ):
        self.req_count = 0
        self.acc = acc
        self.clt = clt
        self.proxy = proxy
        self.impersonate = impersonate
        self.failures = {"transport": 0, "loadshed": 0}

    async def retry(self, kind: str, limit: int) -> bool:
        self.failures[kind] += 1
        if self.failures[kind] >= limit:
            return False
        await asyncio.sleep(2 ** self.failures[kind])
        return True

    def reset_failures(self):
        self.failures = {"transport": 0, "loadshed": 0}

    async def aclose(self):
        await self.clt.aclose()

    async def req(
        self,
        method: str,
        url: str,
        params: ReqParams = None,
        json: Any = None,
    ) -> Response:
        # if code 404 on first try then generate new x-client-transaction-id and retry
        # https://github.com/vladkens/twscrape/issues/248
        path = urlparse(url).path or "/"

        tries = 0
        while tries < 3:
            gen = await XClIdGenStore.get(
                self.acc, proxy=self.proxy, fresh=tries > 0, impersonate=self.impersonate
            )
            hdr = {"x-client-transaction-id": gen.calc(method, path)}
            rep = await self.clt.request(method, url, params=params, json=json, headers=hdr)
            if rep.status_code != 404:
                return rep

            tries += 1
            logger.debug(f"Retrying request with new x-client-transaction-id: {url}")
            await asyncio.sleep(1)

        raise NotFoundAfterSigningError(
            f"HTTP 404 for {path} after {tries} fresh x-client-transaction-ids"
        )


def req_id(rep: Response):
    lr = str(rep.headers.get("x-rate-limit-remaining", -1))
    ll = str(rep.headers.get("x-rate-limit-limit", -1))
    sz = max(len(lr), len(ll))
    lr, ll = lr.rjust(sz), ll.rjust(sz)

    username = getattr(rep, "__username", "<UNKNOWN>")
    return f"{lr}/{ll} - {username}"


def dump_rep(rep: Response):
    count = getattr(dump_rep, "__count", -1) + 1
    setattr(dump_rep, "__count", count)

    acc = getattr(rep, "__username", "<unknown>")
    outfile = f"{count:05d}_{rep.status_code}_{acc}.txt"
    outfile = f"/tmp/twscrape-{TMP_TS}/{outfile}"
    os.makedirs(os.path.dirname(outfile), exist_ok=True)

    msg = []
    msg.append(f"{count:,d} - {req_id(rep)}")
    msg.append(f"{rep.status_code} {rep.request.method} {rep.request.url}")
    msg.append("\n")
    # msg.append("\n".join([str(x) for x in list(rep.request.headers.items())]))
    msg.append("\n".join([str(x) for x in list(rep.headers.items())]))
    msg.append("\n")

    try:
        msg.append(json.dumps(rep.json(), indent=2))
    except json.JSONDecodeError:
        msg.append(rep.text)

    txt = "\n".join(msg)
    with open(outfile, "w") as f:
        f.write(txt)


class QueueClient:
    def __init__(
        self,
        pool: AccountsPool,
        queue: str,
        debug=False,
        proxy: str | None = None,
        impersonate: str | None = None,
    ):
        self.pool = pool
        self.queue = queue
        self.debug = debug
        self.ctx: Ctx | None = None
        self.proxy = proxy
        self.impersonate = impersonate

    def _ctx_for(self, acc: Account) -> "Ctx":
        clt = acc.make_client(proxy=self.proxy, impersonate=self.impersonate)
        return Ctx(acc, clt, proxy=self.proxy, impersonate=self.impersonate)

    async def __aenter__(self):
        await self._get_ctx()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._close_ctx()

    async def _close_ctx(
        self,
        reset_at=-1,
        inactive=False,
        msg: str | None = None,
        global_lock=False,
    ):
        if self.ctx is None:
            return

        ctx, self.ctx, self.req_count = self.ctx, None, 0
        username = ctx.acc.username
        await ctx.aclose()

        if inactive:
            await self.pool.mark_inactive(username, msg, queue=self.queue, req_count=ctx.req_count)
            return

        if reset_at > 0:
            if global_lock:
                await self.pool.lock_until(ctx.acc.username, GLOBAL_LOCK_QUEUE, reset_at)
            await self.pool.lock_until(ctx.acc.username, self.queue, reset_at, ctx.req_count)
            return

        await self.pool.unlock(ctx.acc.username, self.queue, ctx.req_count)

    async def _get_ctx(self):
        if self.ctx:
            return self.ctx

        acc = await self.pool.get_for_queue_or_wait(self.queue)
        if acc is None:
            return None

        self.ctx = self._ctx_for(acc)
        return self.ctx

    async def _move_to_free_account(self) -> bool:
        # A pool without a free account keeps retrying the current one, as before.
        acc = await self.pool.get_for_queue(self.queue)
        if acc is None:
            return False

        await self._close_ctx(utc.ts() + TRANSIENT_API_ERROR_COOLDOWN_SECONDS)
        self.ctx = self._ctx_for(acc)
        return True

    async def _quarantine_refused_search(self, reason: str) -> bool:
        """Keep the refused account out of search, longer for each consecutive refusal, and continue with a free
        account, if any."""
        if self.ctx is None:
            return False
        username = self.ctx.acc.username
        refusals = await self.pool.record_refusal(username, self.queue)
        lock_seconds = search_refusal_lock_seconds(refusals)
        logger.warning(
            f"Search refused for {username} ({reason}, {refusals} in a row); "
            f"no searches with it for {lock_seconds}s"
        )
        await self._close_ctx(utc.ts() + lock_seconds)
        acc = await self.pool.get_for_queue(self.queue)
        if acc is None:
            return False
        self.ctx = self._ctx_for(acc)
        return True

    async def _signed_control_succeeds(self) -> bool:
        """One signed GraphQL read that search-limited accounts still get (UserByScreenName).

        A search-limited account still reads; an account whose every read fails is not search-limited, so it
        gets no search quarantine. Measured live on 2026-11-21: X answered UserByScreenName, TweetDetail
        and UserTweets with 200 also for a random or missing x-client-transaction-id, so this read does not
        detect a broken signature; tests/test_xclid_browser_vectors.py guards the signature instead.
        """
        from .api import GQL_FEATURES, GQL_URL, OP_UserByScreenName, USER_BY_SCREEN_NAME_FEATURES
        from .utils import encode_params

        ctx = self.ctx
        if ctx is None:
            return False
        url = f"{GQL_URL}/{OP_UserByScreenName}"
        params = {
            "variables": {"screen_name": CONTROL_SCREEN_NAME, "withSafetyModeUserFields": True},
            "features": {**GQL_FEATURES, **USER_BY_SCREEN_NAME_FEATURES},
        }
        try:
            gen = await XClIdGenStore.get(ctx.acc, proxy=self.proxy, impersonate=self.impersonate)
            hdr = {"x-client-transaction-id": gen.calc("GET", urlparse(url).path)}
            rep = await ctx.clt.get(url, params=encode_params(params), headers=hdr)
            # Inside the try: any surprise in the answer is an inconclusive read, never an escaping error.
            return has_user_data(rep)
        except Exception as e:
            logger.warning(
                f"Signed control read failed for {ctx.acc.username}: {type(e).__name__}: {e}"
            )
            return False

    async def _search_limit_confirmed(self, reason: str) -> bool:
        """A refused search counts as a search limit only when the signed control read returns user data.

        Otherwise the account cools down for SIGNING_FAILURE_COOLDOWN_SECONDS and the caller fails the request.
        """
        if await self._signed_control_succeeds():
            return True
        username = self.ctx.acc.username if self.ctx else "<unknown>"
        logger.warning(
            f"Search refused for {username} ({reason}) and the signed control read returned no user data: "
            f"not a search limit. Cooling the account for {SIGNING_FAILURE_COOLDOWN_SECONDS}s"
        )
        await self._close_ctx(utc.ts() + SIGNING_FAILURE_COOLDOWN_SECONDS)
        return False

    @staticmethod
    def _has_timeline_data(rep: Response) -> bool:
        try:
            return has_data(rep, rep.json())
        except ValueError:
            return False

    @staticmethod
    def _is_search_refusal(err_msg: str) -> bool:
        return "(-1) Internal server error" in err_msg

    async def _check_rep(self, rep: Response) -> None:
        """
        This function can raise Exception and request will be retried or aborted
        Or if None is returned, response will passed to api parser as is
        """

        if self.debug:
            dump_rep(rep)

        # An X outage, whatever the body (JSON errors, plain text, undecodable bytes or nothing) and whatever the
        # headers: never an account lock or a refusal. Checked before the body or the headers are parsed.
        if 500 <= rep.status_code < 600:
            detail = _outage_detail(rep)
            logger.warning(
                f"Transient X API error detected: {rep.status_code:3d} - {req_id(rep)} - {detail}"
            )
            raise ServiceUnavailableError(
                f"Transient X API error ({rep.status_code}) for {self.queue}: {detail}"
            )

        try:
            res = rep.json()
        except ValueError:  # JSONDecodeError, and UnicodeDecodeError for a body that is not UTF-8
            res: Any = {"_raw": rep.text}

        limit_remaining = _int_header(rep, "x-rate-limit-remaining")
        limit_reset = _int_header(rep, "x-rate-limit-reset")
        rate_limited_429 = rep.status_code == 429
        # limit_max = int(rep.headers.get("x-rate-limit-limit", -1))

        errors: list[str] = []
        malformed_errors = False
        error_payload = res.get("errors") if isinstance(res, dict) else None
        if isinstance(error_payload, list):
            errors = [
                f"({item.get('code', -1)}) {item['message']}"
                for item in error_payload
                if isinstance(item, dict) and "message" in item
            ]
            if error_payload and len(errors) != len(error_payload):
                malformed_errors = True
                errors.append("(-1) Malformed API errors payload")
            errors = list(dict.fromkeys(errors))
        elif error_payload:
            malformed_errors = True
            errors = ["(-1) Malformed API errors payload"]

        err_msg = "; ".join(errors) or "OK"
        log_key = (self.queue, rep.status_code, frozenset(errors))

        log_msg = f"{rep.status_code:3d} - {req_id(rep)} - {err_msg}"
        logger.trace(log_msg)

        if malformed_errors:
            logger.error(f"API malformed errors payload: {log_msg}")
            await self._close_ctx(utc.ts() + UNKNOWN_API_ERROR_COOLDOWN_SECONDS)
            raise UnexpectedApiError(
                f"Malformed X API errors payload ({rep.status_code}) for {self.queue}"
            )

        # for dev: need to add some features in api.py
        if has_error(errors, "(336) The following features cannot be null"):
            raise ApiFeatureUpdateRequiredError(
                "X rejected the configured GraphQL feature flags. "
                f"Update twscrape/api.py and retry. Details: {err_msg}"
            )

        # https://github.com/vladkens/twscrape/issues/211
        suspended = has_error(errors, "(64) Your account is suspended")
        if suspended or has_error(errors, "(326) Authorization: Denied by access control"):
            logger.warning(f"Ban detected: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        # general api rate limit
        if limit_remaining == 0 and limit_reset > utc.ts():
            logger.debug(f"Rate limited: {log_msg}")
            await self._close_ctx(limit_reset)
            raise HandledError()

        # no way to check is account banned in direct way, but this check should work
        rate_limited = has_error(errors, "(88) Rate limit exceeded") and not has_data(rep, res)
        if rate_limited and limit_remaining > 0 and not rate_limited_429:
            logger.warning(f"Ban detected: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        # https://github.com/vladkens/twscrape/issues/351
        if rate_limited_429 or rate_limited:
            logger.debug(f"Rate limited: {log_msg}")
            await self._close_ctx(limit_reset if limit_reset > utc.ts() else utc.ts() + 60 * 15)
            raise HandledError()

        if has_error(errors, "(32) Could not authenticate you"):
            logger.warning(f"Session expired or banned: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        if is_html_edge_block(rep, res):
            logger.warning(
                f"HTML edge block detected: {log_msg}. Cooling queue for {UNKNOWN_API_ERROR_COOLDOWN_SECONDS}s"
            )
            await self._close_ctx(utc.ts() + UNKNOWN_API_ERROR_COOLDOWN_SECONDS)
            raise UnexpectedApiError(
                f"HTML edge block ({rep.status_code}) for {self.queue}. The account stays active for other queues."
            )

        if err_msg == "OK" and rep.status_code == 403:
            logger.warning(
                f"Ambiguous forbidden response: {log_msg}. "
                f"Cooling queue for {UNKNOWN_API_ERROR_COOLDOWN_SECONDS}s"
            )
            await self._close_ctx(utc.ts() + UNKNOWN_API_ERROR_COOLDOWN_SECONDS)
            raise HandledError()

        # something from twitter side - abort all queries, see: https://github.com/vladkens/twscrape/pull/80
        if has_error(errors, "(131) Dependency: Internal error"):
            # looks like when data exists, we can ignore this error
            # https://github.com/vladkens/twscrape/issues/166
            if not has_data(rep, res):
                logger.warning(f"Dependency error (request aborted): {err_msg}")
                raise AbortReqError(f"X API dependency error for {self.queue}: {err_msg}")

        # content not found
        if rep.status_code == 200 and "_Missing: No status found with that ID" in err_msg:
            return  # ignore this error

        # something from twitter side - just ignore it, see: https://github.com/vladkens/twscrape/pull/95
        if rep.status_code == 200 and "Authorization" in err_msg:
            logger.warning(f"Authorization unknown error: {log_msg}")
            return

        if err_msg != "OK":
            if has_data(rep, res):
                LogOnce.throttled(
                    log_key,
                    "DEBUG",
                    f"API warning with data: {rep.status_code:3d} - {self.queue} - {err_msg}",
                )
                return

            if has_error(errors, "(-1) LoadShed"):
                logger.warning(f"LoadShed detected: {log_msg}")
                ctx = self.ctx
                if ctx is not None and await ctx.retry("loadshed", LOADSHED_RETRY_LIMIT):
                    raise RetryReqError()

                logger.warning(
                    f"LoadShed persisted: {log_msg}. "
                    f"Cooling account for {LOADSHED_COOLDOWN_SECONDS}s"
                )
                await self._close_ctx(
                    utc.ts() + LOADSHED_COOLDOWN_SECONDS,
                    global_lock=True,
                )
                raise HandledError()

            # Only an HTTP 200 answer without timeline data is a refusal; a 5xx stays transient.
            if (
                self.queue == SEARCH_QUEUE
                and rep.status_code == 200
                and self._is_search_refusal(err_msg)
            ):
                raise SearchRefusedError(
                    f"Transient X API error ({rep.status_code}) for {self.queue}: {err_msg}"
                )

            if is_transient_api_error(err_msg):
                logger.warning(f"Transient X API error detected: {log_msg}")
                raise ServiceUnavailableError(
                    f"Transient X API error ({rep.status_code}) for {self.queue}: {err_msg}"
                )

            logger.error(f"API unknown error: {log_msg}")
            await self._close_ctx(utc.ts() + UNKNOWN_API_ERROR_COOLDOWN_SECONDS)
            raise UnexpectedApiError(
                f"Unhandled X API error ({rep.status_code}) for {self.queue}: {err_msg}"
            )

        try:
            rep.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"Unhandled API response code: {log_msg}")
            await self._close_ctx(utc.ts() + 60 * 15)  # 15 minutes
            raise HandledError()

    async def get(self, url: str, params: ReqParams = None) -> Response | None:
        return await self.req("GET", url, params=params)

    async def post(self, url: str, json: Any = None) -> Response | None:
        return await self.req("POST", url, json=json)

    async def req(
        self,
        method: str,
        url: str,
        params: ReqParams = None,
        json: Any = None,
    ) -> Response | None:
        unknown_retry, transient_api_retry, refused = 0, 0, 0
        held: list[str] = []

        while True:
            ctx = await self._get_ctx()  # not need to close client, class implements __aexit__
            if ctx is None:
                return None

            if not has_required_cookies(ctx.acc.cookies):
                msg = "Missing authentication cookies"
                logger.warning(f"{msg}; username={ctx.acc.username}; queue={self.queue}")
                await self._close_ctx(inactive=True, msg=msg)
                continue

            try:
                rep = await ctx.req(method, url, params=params, json=json)
                setattr(rep, "__username", ctx.acc.username)
                await self._check_rep(rep)
                if self.queue == SEARCH_QUEUE and self._has_timeline_data(rep):
                    await self.pool.reset_refusals(ctx.acc.username, self.queue)

                ctx.req_count += 1  # count only successful
                ctx.reset_failures()
                unknown_retry, transient_api_retry = 0, 0
                return rep
            except NotFoundAfterSigningError as e:
                if self.queue != SEARCH_QUEUE:
                    raise
                if not await self._search_limit_confirmed("HTTP 404 after fresh signing"):
                    raise
                refused += 1
                if not await self._quarantine_refused_search("HTTP 404 after fresh signing") or (
                    refused >= TRANSIENT_API_ERROR_RETRY_LIMIT
                ):
                    raise SearchRefusedError(f"X refused {self.queue} with HTTP 404: {e}") from e
            except SearchRefusedError as e:
                if not await self._search_limit_confirmed("(-1) Internal server error"):
                    raise ServiceUnavailableError(str(e)) from e
                refused += 1
                if not await self._quarantine_refused_search("(-1) Internal server error") or (
                    refused >= TRANSIENT_API_ERROR_RETRY_LIMIT
                ):
                    raise
            except AbortReqError:
                raise
            except ApiFeatureUpdateRequiredError:
                raise
            except ServiceUnavailableError as e:
                transient_api_retry += 1
                if transient_api_retry >= TRANSIENT_API_ERROR_RETRY_LIMIT:
                    # Every account tried failed, so X failed as a whole: release them.
                    for username in held:
                        await self.pool.unlock(username, self.queue)
                    raise e
                # X refuses SearchTimeline for some accounts only (vladkens/twscrape#367).
                # Other operations keep retrying the same account.
                if self.queue == "SearchTimeline" and await self._move_to_free_account():
                    held.append(ctx.acc.username)
                await asyncio.sleep(1)
            except UnexpectedApiError:
                raise
            except RetryReqError:
                continue
            except HandledError:
                # retry with new account
                unknown_retry, transient_api_retry = 0, 0
                continue
            except XClIdAccountError as e:
                logger.warning(f"{e}; username={ctx.acc.username}; queue={self.queue}")
                await self._close_ctx(utc.ts() + UNKNOWN_API_ERROR_COOLDOWN_SECONDS)
                continue
            except XClIdParseError as e:
                logger.error(f"{e}; username={ctx.acc.username}; queue={self.queue}")
                await self._close_ctx()
                raise
            except httpx.TransportError as e:
                if await ctx.retry("transport", TRANSPORT_ERROR_RETRY_LIMIT):
                    continue

                logger.warning(
                    f"Transport error; username={ctx.acc.username}; queue={self.queue}; "
                    f"error={type(e).__name__}: {e}. "
                    f"Cooling account for {TRANSPORT_ERROR_COOLDOWN_SECONDS}s"
                )
                await self._close_ctx(utc.ts() + TRANSPORT_ERROR_COOLDOWN_SECONDS)
                unknown_retry, transient_api_retry = 0, 0
                continue
            except Exception as e:
                unknown_retry += 1
                if unknown_retry >= 3:
                    msg = [
                        "Unknown error. Account timeouted for 15 minutes.",
                        "Create issue please: https://github.com/alexander-schneider/twscrape/issues",
                        f"If it mistake, you can unlock accounts with `twscrape reset_locks`. Err: {type(e)}: {e}",
                    ]

                    logger.warning(" ".join(msg))
                    await self._close_ctx(utc.ts() + 60 * 15)  # 15 minutes
