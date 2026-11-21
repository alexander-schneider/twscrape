"""An httpx transport that sends requests through curl-cffi with a browser TLS and HTTP/2 fingerprint.

twscrape keeps its httpx client (cookies, redirects, headers); only the wire is curl-impersonate. The browser
profile owns the user agent, accept headers and client hints, so a stored account user agent never reaches X
with a fingerprint that contradicts it (vladkens/twscrape#337, #363).
"""

import httpx
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

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


class CurlTransport(httpx.AsyncBaseTransport):
    def __init__(self, impersonate: str, proxy: str | None = None, timeout: float = 30.0):
        self._session = AsyncSession(impersonate=impersonate, proxy=proxy, timeout=timeout)  # type: ignore[arg-type]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = [
            (key, value)
            for key, value in request.headers.multi_items()
            if key.lower() not in BROWSER_PROFILE_HEADERS
        ]
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
            raise httpx.TransportError(f"curl-cffi request failed: {error}") from error

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

    async def aclose(self) -> None:
        await self._session.close()
