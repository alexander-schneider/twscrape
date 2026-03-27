import httpx
import pytest
from bs4 import BeautifulSoup

from twscrape.xclid import (
    XClIdError,
    XClIdGen,
    get_scripts_list,
    get_tw_page_text,
    load_keys,
    parse_anim_idx,
)


def test_get_scripts_list_malformed_json():
    # Test case with malformed JSON (unquoted keys)
    malformed_text_content = (
        'node_modules_pnpm_ws_8_18_0_node_modules_ws_browser_js:"12345",other_key:"67890"'
    )
    malformed_text = (
        'stuff... e=>e+"."+' + "{" + malformed_text_content + "}" + '[e]+"a.js"... stuff'
    )

    scripts = list(get_scripts_list(malformed_text))

    assert len(scripts) == 2
    assert (
        "https://abs.twimg.com/responsive-web/client-web/node_modules_pnpm_ws_8_18_0_node_modules_ws_browser_js.12345a.js"
        in scripts
    )
    assert "https://abs.twimg.com/responsive-web/client-web/other_key.67890a.js" in scripts


def test_get_scripts_list_normal_json():
    # Test case with normal JSON (quoted keys)
    normal_text_content = '"normal_key":"12345","another_key":"67890"'
    normal_text = 'stuff... e=>e+"."+' + "{" + normal_text_content + "}" + '[e]+"a.js"... stuff'

    scripts = list(get_scripts_list(normal_text))

    assert len(scripts) == 2
    assert "https://abs.twimg.com/responsive-web/client-web/normal_key.12345a.js" in scripts
    assert "https://abs.twimg.com/responsive-web/client-web/another_key.67890a.js" in scripts


def test_get_scripts_list_current_xcom_html():
    html = """
    <html>
      <head>
        <link rel="preload" as="script" href="https://abs.twimg.com/responsive-web/client-web/vendor.d74f1a3a.js" />
        <script src="//abs.twimg.com/responsive-web/client-web/main.540aea7a.js"></script>
      </head>
      <body>
        <script>
          window.__SOMETHING__ = {"ondemand.s":"246a373","icons.25":"78d9a92"};
        </script>
      </body>
    </html>
    """

    scripts = list(get_scripts_list(html))

    assert "https://abs.twimg.com/responsive-web/client-web/vendor.d74f1a3a.js" in scripts
    assert "https://abs.twimg.com/responsive-web/client-web/main.540aea7a.js" in scripts
    assert "https://abs.twimg.com/responsive-web/client-web/ondemand.s.246a373a.js" in scripts


def test_get_scripts_list_falls_back_when_legacy_blob_is_unparseable():
    html = """
    <html>
      <head>
        <script>
          // Old marker still present in some bundles, but blob is not valid JSON anymore.
          x = e=>e+"." + {not-valid: no, still-broken: yes}[e]+"a.js";
        </script>
        <link rel="preload" as="script" href="https://abs.twimg.com/responsive-web/client-web/vendor.d74f1a3a.js" />
      </head>
      <body>
        <script>
          window.__SOMETHING__ = {"ondemand.s":"246a373"};
        </script>
      </body>
    </html>
    """

    scripts = list(get_scripts_list(html))

    assert "https://abs.twimg.com/responsive-web/client-web/vendor.d74f1a3a.js" in scripts
    assert "https://abs.twimg.com/responsive-web/client-web/ondemand.s.246a373a.js" in scripts


@pytest.mark.asyncio
async def test_parse_anim_idx_current_xcom_html(monkeypatch):
    html = """
    <html>
      <head>
        <script src="https://abs.twimg.com/responsive-web/client-web/main.540aea7a.js"></script>
      </head>
      <body>
        <script>
          window.__SOMETHING__ = {"ondemand.s":"246a373"};
        </script>
      </body>
    </html>
    """
    ondemand = """
    const z = n[4], a = n[32], b = n[25], c = n[42];
    if (!$r) {
        const [row, frame] = [ir.foo(n[4],16), ir.bar(ir.baz(n[32],16), ir.qux(n[25],16), ir.quux(n[42],16))];
    }
    """

    async def fake_get_tw_page_text(url: str, clt=None):
        assert url == "https://abs.twimg.com/responsive-web/client-web/ondemand.s.246a373a.js"
        return ondemand

    monkeypatch.setattr("twscrape.xclid.get_tw_page_text", fake_get_tw_page_text)

    assert await parse_anim_idx(html) == [4, 32, 25, 42]


@pytest.mark.asyncio
async def test_parse_anim_idx_falls_back_to_main_bundle_when_ondemand_missing(monkeypatch):
    html = """
    <html>
      <head>
        <script src="https://abs.twimg.com/responsive-web/client-web/vendor.f1dc7e4a.js"></script>
        <script src="https://abs.twimg.com/responsive-web/client-web/main.29dcc91a.js"></script>
      </head>
    </html>
    """
    bundles = {
        "https://abs.twimg.com/responsive-web/client-web/vendor.f1dc7e4a.js": "window.vendor = true",
        "https://abs.twimg.com/responsive-web/client-web/main.29dcc91a.js": "function tid(){return (h[7],16)}",
    }

    async def fake_get_tw_page_text(url: str, clt=None):
        return bundles[url]

    monkeypatch.setattr("twscrape.xclid.get_tw_page_text", fake_get_tw_page_text)

    assert await parse_anim_idx(html) == [7]


@pytest.mark.asyncio
async def test_parse_anim_idx_skips_failing_bundle_fetches(monkeypatch):
    html = """
    <html>
      <head>
        <script src="https://abs.twimg.com/responsive-web/client-web/main.29dcc91a.js"></script>
      </head>
      <body>
        <script>
          window.__SOMETHING__ = {"ondemand.s":"246a373"};
        </script>
      </body>
    </html>
    """
    request = httpx.Request(
        "GET", "https://abs.twimg.com/responsive-web/client-web/ondemand.s.246a373a.js"
    )
    response = httpx.Response(404, request=request)

    async def fake_get_tw_page_text(url: str, clt=None):
        if "ondemand.s" in url:
            raise httpx.HTTPStatusError("not found", request=request, response=response)
        return "function tid(){return (h[7],16)}"

    monkeypatch.setattr("twscrape.xclid.get_tw_page_text", fake_get_tw_page_text)

    assert await parse_anim_idx(html) == [7]


@pytest.mark.asyncio
async def test_get_tw_page_text_closes_owned_client(monkeypatch):
    class FakeResponse:
        def __init__(self, text: str):
            self.text = text

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self):
            self.closed = False

        async def get(self, url: str):
            assert url == "https://x.com/tesla"
            return FakeResponse("<html>ok</html>")

        async def aclose(self):
            self.closed = True

    fake_client = FakeClient()
    monkeypatch.setattr("twscrape.xclid._make_client", lambda: fake_client)

    assert await get_tw_page_text("https://x.com/tesla") == "<html>ok</html>"
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_xclid_create_retries_transient_generation_errors(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    fake_client = FakeClient()
    attempts = {"count": 0}

    async def fake_get_tw_page_text(url: str, clt=None):
        return "<html></html>"

    async def fake_load_keys(page_text: str, soup, clt=None):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise XClIdError("transient")
        return [1] * 64, "anim-key"

    async def fake_sleep(_seconds: float):
        return None

    monkeypatch.setattr("twscrape.xclid._make_client", lambda: fake_client)
    monkeypatch.setattr("twscrape.xclid.get_tw_page_text", fake_get_tw_page_text)
    monkeypatch.setattr("twscrape.xclid.load_keys", fake_load_keys)
    monkeypatch.setattr("twscrape.xclid.asyncio.sleep", fake_sleep)

    gen = await XClIdGen.create()

    assert attempts["count"] == 2
    assert gen.anim_key == "anim-key"
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_load_keys_uses_raw_html_for_anim_idx(monkeypatch):
    html = """
    <html>
      <head>
        <meta name="twitter-site-verification" content="QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=" />
      </head>
      <body>
        <svg id="loading-x-anim-test">
          <g>
            <path></path>
            <path d="M 0 0 C 1 2 3 4 5 6 7 8 9 10 11 12 13 14"></path>
          </g>
        </svg>
        <script>
          window.__SOMETHING__ = {"ondemand.s":"246a373"};
        </script>
      </body>
    </html>
    """
    seen = {}

    async def fake_parse_anim_idx(text: str, clt=None):
        seen["text"] = text
        return [4, 32, 25, 42]

    monkeypatch.setattr("twscrape.xclid.parse_anim_idx", fake_parse_anim_idx)
    monkeypatch.setattr("twscrape.xclid.parse_vk_bytes", lambda soup: [1] * 64)
    monkeypatch.setattr(
        "twscrape.xclid.parse_anim_arr", lambda soup, vk_bytes: [[1.0] * 15 for _ in range(16)]
    )
    monkeypatch.setattr("twscrape.xclid.cacl_anim_key", lambda frame_row, frame_dur: "anim-key")

    vk_bytes, anim_key = await load_keys(html, BeautifulSoup(html, "html.parser"))

    assert seen["text"] == html
    assert len(vk_bytes) == 64
    assert anim_key == "anim-key"
