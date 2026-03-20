import pytest
from bs4 import BeautifulSoup

from twscrape.xclid import get_scripts_list, load_keys, parse_anim_idx


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

    async def fake_parse_anim_idx(text: str):
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
