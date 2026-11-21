"""The animation key must match what Chromium computes, or X rejects the transaction id with an empty 404.

Before this test, 441 of the 1000 recorded keys diverged: colour channels above 255 printed as three hex digits,
plain bisection moved an eased value, and matrix entries were rounded from the raw double instead of their printed
form (Lqm1/x-client-transaction-id#26).
"""

import json
import re
from pathlib import Path

import bs4

from twscrape.xclid import animation_key, parse_anim_arr, parse_vk_bytes

DATA = Path(__file__).parent / "browser_capture" / "xtid"
INDICES = [14, 20, 19, 30]


def test_animation_keys_match_chromium_for_all_recorded_keys():
    page = (DATA / "live_page.html").read_text()
    vectors = json.loads((DATA / "vectors.json").read_text())["keys"]

    mismatches = []
    for key, expected in vectors:
        html = re.sub(
            r'(<meta name="twitter-site-verification" content=")[^"]*(")',
            rf"\g<1>{key}\g<2>",
            page,
        )
        soup = bs4.BeautifulSoup(html, "html.parser")
        vk_bytes = parse_vk_bytes(soup)
        got = animation_key(vk_bytes, INDICES, parse_anim_arr(soup, vk_bytes))
        if got != expected:
            mismatches.append((key, expected, got))

    assert len(vectors) == 1000
    assert mismatches == []
