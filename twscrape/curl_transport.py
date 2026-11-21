"""An httpx transport that sends requests through curl-cffi with a browser TLS and HTTP/2 fingerprint.

twscrape keeps its httpx client (cookies, redirects, headers); only the wire is curl-impersonate. The browser
profile owns the user agent, accept headers and client hints, so a stored account user agent never reaches X
with a fingerprint that contradicts it (vladkens/twscrape#337, #363).
"""

import json
from urllib.parse import quote

import httpx
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

from .redaction import raise_without_context

# Headers the browser profile sets; httpx defaults and stored account values would contradict it.
BROWSER_PROFILE_HEADERS = {
    "user-agent",
    "accept",
    "accept-encoding",
    "connection",
    "host",
    "content-length",
}
# curl decodes the body, so the response must not announce an encoding or the encoded length.
DECODED_RESPONSE_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}
API_PATH_PREFIXES = ("/i/api/", "/graphql/", "/1.1/")
# encodeURIComponent keeps only A-Z a-z 0-9 - _ . ! ~ * ' ( )
ENCODE_URI_COMPONENT_SAFE = "!~*'()"
SEARCH_PRODUCT_FILTERS = {
    "Latest": "&f=live",
    "People": "&f=user",
    "Media": "&f=media",
    "Lists": "&f=list",
}


def _api_referer(url: httpx.URL) -> str:
    """The page the web client sits on: the search page for a search, the home page otherwise."""
    if url.path.endswith("/SearchTimeline"):
        try:
            variables = json.loads(url.params.get("variables") or "{}")
        except ValueError:
            variables = {}
        query = variables.get("rawQuery") if isinstance(variables, dict) else None
        if query:
            product = SEARCH_PRODUCT_FILTERS.get(str(variables.get("product")), "")
            encoded = quote(query, safe=ENCODE_URI_COMPONENT_SAFE)
            return f"https://x.com/search?q={encoded}&src=typed_query{product}"
    return "https://x.com/"


def api_fetch_headers(request: httpx.Request) -> dict[str, str | None]:
    """Headers of the web client's fetch() calls, replacing the page-load defaults of the browser profile.

    curl-cffi's profile describes a navigation (sec-fetch-mode navigate, an HTML accept, upgrade-insecure-
    requests). On API calls that contradicts the browser fingerprint; a None value removes a profile header.
    """
    url = request.url
    if not url.path.startswith(API_PATH_PREFIXES):
        return {}
    headers: dict[str, str | None] = {
        "accept": "*/*",
        "sec-fetch-site": "same-site" if url.host.startswith("api.") else "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "priority": "u=1, i",
        "referer": _api_referer(url),
        "upgrade-insecure-requests": None,
        "sec-fetch-user": None,
    }
    if request.method == "POST":
        headers["origin"] = "https://x.com"
    return headers


class CurlTransport(httpx.AsyncBaseTransport):
    def __init__(self, impersonate: str, proxy: str | None = None, timeout: float = 30.0):
        self._proxy = proxy
        self._session = AsyncSession(impersonate=impersonate, proxy=proxy, timeout=timeout)  # type: ignore[arg-type]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers: dict[str, str | None] = {
            key.lower(): value
            for key, value in request.headers.multi_items()
            if key.lower() not in BROWSER_PROFILE_HEADERS
        }
        headers.update(api_fetch_headers(request))
        body = await request.aread()
        try:
            rep = await self._session.request(
                request.method,  # type: ignore[arg-type]
                str(request.url),
                headers=headers,
                data=body or None,
                allow_redirects=False,
            )
        except RequestsError as error:
            # libcurl echoes the proxy URL (with its userinfo) in some errors: with a proxy, keep only the type.
            message = (
                f"curl-cffi request failed ({type(error).__name__}); details withheld because a proxy is configured"
                if self._proxy
                else f"curl-cffi request failed: {error}"
            )
        else:
            return httpx.Response(
                rep.status_code,
                headers=[
                    (key, value)
                    for key, value in rep.headers.multi_items()
                    if value is not None and key.lower() not in DECODED_RESPONSE_HEADERS
                ],
                content=rep.content,
                request=request,
            )
        # __context__ would otherwise keep the original libcurl error (on Python 3.10 even after the except block).
        raise_without_context(httpx.TransportError(message))

    async def aclose(self) -> None:
        await self._session.close()
