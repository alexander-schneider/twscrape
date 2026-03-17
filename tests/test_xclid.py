
import pytest
from twscrape.xclid import get_scripts_list, parse_anim_idx

def test_get_scripts_list_malformed_json():
    # Test case with malformed JSON (unquoted keys)
    malformed_text_content = 'node_modules_pnpm_ws_8_18_0_node_modules_ws_browser_js:"12345",other_key:"67890"'
    malformed_text = 'stuff... e=>e+"."+' + '{' + malformed_text_content + '}' + '[e]+"a.js"... stuff'
    
    scripts = list(get_scripts_list(malformed_text))
    
    assert len(scripts) == 2
    assert "https://abs.twimg.com/responsive-web/client-web/node_modules_pnpm_ws_8_18_0_node_modules_ws_browser_js.12345a.js" in scripts
    assert "https://abs.twimg.com/responsive-web/client-web/other_key.67890a.js" in scripts

def test_get_scripts_list_normal_json():
    # Test case with normal JSON (quoted keys)
    normal_text_content = '"normal_key":"12345","another_key":"67890"'
    normal_text = 'stuff... e=>e+"."+' + '{' + normal_text_content + '}' + '[e]+"a.js"... stuff'
    
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
