"""Finding the signing indices in X's hashed x-web build (vladkens/twscrape#379, #380).

The x-web page links only `x-web/x-web/entry-client-<hash>.js`. No chunk is named `sign.o` or `ondemand.s` any
more: the signing chunk is loaded with `import(x).then(m => m.default())` from a chunk the entry imports. Ways the
search can fail, covered below:
- it stops at the page scripts and raises "Couldn't get XClientTxId indices" for accounts served x-web;
- it takes the first chunk with a single `(a[N], 16)` match, which an unrelated large chunk can have, and signs with
  wrong indices that X answers with an empty 404;
- it follows imports without limit and downloads the whole bundle.
"""

import httpx
import pytest

from twscrape import xclid

BASE = "https://abs.twimg.com/x-web/x-web"
ENTRY = f"{BASE}/entry-client-Ab1.js"
PAGE = f'<html><head><script type="module" src="{ENTRY}"></script></head></html>'
SIGN_JS = "let a=t;x=(a[3], 16);y=(a[7], 16);z=(a[1], 16)"


def _client(routes: dict[str, str], requested: list[str]) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url in routes:
            return httpx.Response(200, text=routes[url])
        return httpx.Response(404, text="")

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


async def test_signing_chunk_two_levels_below_the_entry():
    requested: list[str] = []
    routes = {
        ENTRY: 'import{a as b}from"./assets/Lvl_1.js";import"./assets/Css1.js";',
        f"{BASE}/assets/Lvl_1.js": "function f(){return import(`./Sg_9x.js`).then(e=>e.default())}",
        f"{BASE}/assets/Css1.js": "",
        f"{BASE}/assets/Sg_9x.js": SIGN_JS,
    }
    async with _client(routes, requested) as client:
        assert await xclid.parse_anim_idx(PAGE, client) == [3, 7, 1]


async def test_a_chunk_with_one_index_is_not_the_signing_chunk():
    requested: list[str] = []
    routes = {
        ENTRY: 'import{a}from"./assets/Lvl_1.js";x=(a[9], 16)',
        f"{BASE}/assets/Lvl_1.js": (
            'import("./One.js").then(e=>e.default());'
            "import(`./None.js`).then(n=>n.default());"
            "import('./Sign.js').then((t) => t.default());"
            'import("./Plain.js").then(e=>e.foo())'
        ),
        f"{BASE}/assets/One.js": "x=(a[5], 16)",
        f"{BASE}/assets/None.js": "nothing here",
        f"{BASE}/assets/Sign.js": SIGN_JS,
        f"{BASE}/assets/Plain.js": SIGN_JS,
    }
    async with _client(routes, requested) as client:
        assert await xclid.parse_anim_idx(PAGE, client) == [3, 7, 1]
    assert f"{BASE}/assets/Plain.js" not in requested


async def test_import_depth_is_limited():
    requested: list[str] = []
    routes = {
        ENTRY: 'import"./assets/L1.js";',
        f"{BASE}/assets/L1.js": 'import"./L2.js";',
        f"{BASE}/assets/L2.js": 'import"./L3.js";',
        f"{BASE}/assets/L3.js": "import(`./Sign.js`).then(e=>e.default())",
        f"{BASE}/assets/Sign.js": SIGN_JS,
    }
    async with _client(routes, requested) as client:
        with pytest.raises(xclid.XClIdParseError):
            await xclid.parse_anim_idx(PAGE, client)
    assert f"{BASE}/assets/L3.js" not in requested


async def test_legacy_named_ondemand_chunk_still_wins():
    requested: list[str] = []
    legacy = "https://abs.twimg.com/responsive-web/client-web/main.abc.js"
    ondemand = "https://abs.twimg.com/responsive-web/client-web/ondemand.s.deadbeefa.js"
    routes = {legacy: 'var u="ondemand.s.deadbeefa.js"', ondemand: SIGN_JS}
    page = f'<html><script src="{legacy}"></script></html>'
    async with _client(routes, requested) as client:
        assert await xclid.parse_anim_idx(page, client) == [3, 7, 1]
