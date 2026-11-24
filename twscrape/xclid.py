import asyncio
import base64
from decimal import ROUND_FLOOR, Decimal
import hashlib
import json
import math
import random
import re
import time
from http.cookiejar import Cookie
from urllib.parse import urljoin, urlparse

import bs4
import httpx
from fake_useragent import UserAgent


class XClIdError(Exception):
    pass


class XClIdAccountError(XClIdError):
    pass


class XClIdParseError(XClIdError):
    pass


def _make_client(
    proxy: str | None = None,
    user_agent: str | None = None,
    cookies: dict[str, str] | None = None,
    impersonate: str | None = None,
) -> httpx.AsyncClient:
    from .account import make_transport, resolve_impersonate

    headers = {"user-agent": user_agent or UserAgent().chrome}
    transport = make_transport(proxy, resolve_impersonate(impersonate))
    client = httpx.AsyncClient(headers=headers, follow_redirects=True, transport=transport)
    if cookies:
        for key, value in cookies.items():
            client.cookies.jar.set_cookie(_make_secure_x_cookie(key, value))
    return client


def _make_secure_x_cookie(name: str, value: str) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=".x.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


async def get_tw_page_text(url: str, clt: httpx.AsyncClient | None = None):
    owns_client = clt is None
    clt = clt or _make_client()

    try:
        rep = await clt.get(url)
        rep.raise_for_status()
        if ">document.location =" not in rep.text:
            return rep.text

        url = rep.text.split('document.location = "')[1].split('"')[0]
        rep = await clt.get(url)
        rep.raise_for_status()
        if 'action="https://x.com/x/migrate" method="post"' not in rep.text:
            return rep.text

        data = {}
        for x in rep.text.split("<input")[1:]:
            name = x.split('name="')[1].split('"')[0]
            value = x.split('value="')[1].split('"')[0]
            data[name] = value

        rep = await clt.post("https://x.com/x/migrate", json=data)
        rep.raise_for_status()

        return rep.text
    finally:
        if owns_client:
            await clt.aclose()


def script_url(k: str, v: str):
    return f"https://abs.twimg.com/responsive-web/client-web/{k}.{v}.js"


def _parse_legacy_scripts_map(scripts: str):
    try:
        data = json.loads(scripts)
    except json.decoder.JSONDecodeError as e:
        # Fix unquoted keys like: node_modules_pnpm_ws_8_18_0_node_modules_ws_browser_js
        # Twitter started returning malformed JSON with unquoted keys
        try:
            fixed_scripts = re.sub(
                r"([,\{])(\s*)([\w]+_[\w_]+)(\s*):",
                r'\1\2"\3"\4:',
                scripts,
            )
            data = json.loads(fixed_scripts)
        except Exception:
            raise XClIdParseError("Failed to parse scripts") from e

    for k, v in data.items():
        yield script_url(k, f"{v}a")


def _normalize_asset_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"

    return url


def _is_allowed_script_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False

    host = (parsed.hostname or "").lower()
    return host in {"x.com", "twitter.com", "abs.twimg.com"}


def _parse_current_html_scripts(text: str):
    soup = bs4.BeautifulSoup(text, "html.parser")
    seen: set[str] = set()

    for url in re.findall(r"https://[\w.-]+/x-web/[\w./-]+\.js", text):
        if _is_allowed_script_url(url) and url not in seen:
            seen.add(url)
            yield url

    for tag in soup.select("script[src], link[as='script'][href]"):
        url = tag.get("src") or tag.get("href")
        if not url:
            continue

        normalized = _normalize_asset_url(str(url))
        if normalized not in seen:
            seen.add(normalized)
            yield normalized

    ondemand_match = re.search(r"""['"]ondemand\.s['"]\s*:\s*['"]([\w]+)['"]""", text)
    if ondemand_match:
        normalized = script_url("ondemand.s", f"{ondemand_match.group(1)}a")
        if normalized not in seen:
            yield normalized


def _parse_chunk_map_scripts(text: str):
    mappings: dict[str, list[str]] = {}
    for match in re.finditer(r'(\d+):"([^"]+)"', text):
        chunk_id, value = match.groups()
        mappings.setdefault(chunk_id, []).append(value)

    for values in mappings.values():
        name = next((x for x in values if not re.fullmatch(r"[0-9a-f]{7,}", x)), None)
        hash_value = next((x for x in values if re.fullmatch(r"[0-9a-f]{7,}", x)), None)
        if name and hash_value:
            yield script_url(name, f"{hash_value}a")


# The page's application entry: the x-web `entry-client[-...]` module or the responsive-web `main.<hash>` bundle.
APP_ENTRY_RE = re.compile(r"/(?:entry-client(?:-[\w.-]+)?|main\.[\w]+)\.js(?:[?#].*)?$")
LOGGED_OUT_ENTRY_RE = re.compile(r"/entry-client-logged-out(?:[-.][\w.-]+)?\.js(?:[?#].*)?$")
# `type` values a browser runs a `<script src>` with; an absent or empty type is JavaScript too.
EXECUTABLE_SCRIPT_TYPES = {"", "module", "text/javascript", "application/javascript"}


def serves_logged_out_app(text: str) -> bool:
    """Whether the page runs X's logged-out web app: its executable application entries (`<script src>`) are all
    the logged-out entry. A preload, a URL in page data or a logged-in entry beside it is inconclusive (False)."""
    soup = bs4.BeautifulSoup(text, "html.parser")
    entries = []
    for tag in soup.select("script[src]"):
        kind = str(tag.get("type") or "").strip().lower()
        url = _normalize_asset_url(str(tag.get("src")))
        if kind in EXECUTABLE_SCRIPT_TYPES and APP_ENTRY_RE.search(url):
            entries.append(url)
    return bool(entries) and all(LOGGED_OUT_ENTRY_RE.search(url) for url in entries)


def get_scripts_list(text: str):
    marker = 'e=>e+"."+'
    suffix = '[e]+"a.js"'
    if marker in text and suffix in text:
        scripts = text.split(marker, 1)[1].split(suffix, 1)[0]
        try:
            yield from _parse_legacy_scripts_map(scripts)
            return
        except Exception:
            pass

    urls = list(_parse_current_html_scripts(text))
    seen = set(urls)
    for url in _parse_chunk_map_scripts(text):
        if url not in seen:
            urls.append(url)
            seen.add(url)

    if urls:
        if serves_logged_out_app(text):
            raise XClIdAccountError("Logged-out X web app")
        yield from urls
        return

    raise XClIdParseError("Failed to parse scripts")


# MARK: XClientTxId parsing

# Code mostly taken from https://github.com/iSarabjitDhiman/XClientTransaction (MIT licensed)
# Many thanks to @SarabjitDhiman for the original code and investigating the algorithm.
# Articles with more details:
# https://antibot.blog/posts/1741552025433
# https://antibot.blog/posts/1741552092462
# https://antibot.blog/posts/1741552163416


INDICES_REGEX = re.compile(r"(\(\w{1}\[(\d{1,2})\],\s*16\))+", flags=(re.VERBOSE | re.MULTILINE))
INDICES_FILE_RE = re.compile(r"(?:\.{0,2}/)?[\w./-]*?\b(?:ondemand\.s|sign\.o)[\w.-]*\.js")


class Cubic:
    """Cubic Bezier easing evaluated like Chromium's gfx::CubicBezier (ui/gfx/geometry/cubic_bezier.cc).

    X runs the easing as a CSS animation in the browser, so the value to match is Chromium's: a spline-sampled
    first guess, up to 4 Newton steps, then bisection to 1e-7. Plain bisection to 1e-5 moves a printed digit of
    the animation key for some curves (Lqm1/x-client-transaction-id#26).
    """

    EPSILON = 1e-7
    NEWTON_ITERATIONS = 4
    SPLINE_SAMPLES = 11
    SPLINE_STEP = 1.0 / (SPLINE_SAMPLES - 1)

    def __init__(self, curves: list[float]):
        x1, y1, x2, y2 = curves
        self.cx = 3.0 * x1
        self.bx = 3.0 * (x2 - x1) - self.cx
        self.ax = 1.0 - self.cx - self.bx
        self.cy = 3.0 * y1
        self.by = 3.0 * (y2 - y1) - self.cy
        self.ay = 1.0 - self.cy - self.by

        if x1 > 0:
            self.start_gradient = y1 / x1
        elif y1 == 0 and x2 > 0:
            self.start_gradient = y2 / x2
        elif y1 == 0 and y2 == 0:
            self.start_gradient = 1.0
        else:
            self.start_gradient = 0.0

        if x2 < 1:
            self.end_gradient = (y2 - 1) / (x2 - 1)
        elif y2 == 1 and x1 < 1:
            self.end_gradient = (y1 - 1) / (x1 - 1)
        elif y2 == 1 and y1 == 1:
            self.end_gradient = 1.0
        else:
            self.end_gradient = 0.0

        self.spline_samples = [self._x(i * self.SPLINE_STEP) for i in range(self.SPLINE_SAMPLES)]

    def get_value(self, time: float) -> float:
        if time < 0:
            return self.start_gradient * time
        if time > 1:
            return 1.0 + self.end_gradient * (time - 1.0)
        return self._y(self._solve_x(time))

    def _x(self, t: float) -> float:
        return ((self.ax * t + self.bx) * t + self.cx) * t

    def _y(self, t: float) -> float:
        return ((self.ay * t + self.by) * t + self.cy) * t

    def _dx(self, t: float) -> float:
        return (3.0 * self.ax * t + 2.0 * self.bx) * t + self.cx

    def _solve_x(self, x: float) -> float:
        t0, t1, t2 = 0.0, 1.0, x
        for i in range(1, self.SPLINE_SAMPLES):
            if x <= self.spline_samples[i]:
                t1 = self.SPLINE_STEP * i
                t0 = t1 - self.SPLINE_STEP
                t2 = t0 + (t1 - t0) * (x - self.spline_samples[i - 1]) / (
                    self.spline_samples[i] - self.spline_samples[i - 1]
                )
                break

        # Chromium also tests the residual of the point before the last Newton step.
        x2 = 0.0
        for _ in range(self.NEWTON_ITERATIONS):
            x2 = self._x(t2) - x
            if abs(x2) < self.EPSILON:
                return t2
            d2 = self._dx(t2)
            if abs(d2) < self.EPSILON:
                break
            t2 -= x2 / d2
        if abs(x2) < self.EPSILON:
            return t2

        while t0 < t1:
            x2 = self._x(t2)
            if abs(x2 - x) < self.EPSILON:
                return t2
            if x > x2:
                t0 = t2
            else:
                t1 = t2
            t2 = (t1 + t0) * 0.5
        return t2


def js_round(value: float) -> int:
    """JavaScript's Math.round: halves round up, unlike Python's round()."""
    return math.floor(value + 0.5)


def js_to_fixed(value: float, digits: int) -> float:
    """Number(value.toFixed(digits)): from the exact binary value, a tie goes to the larger number."""
    scale = Decimal(10) ** digits
    scaled = Decimal(value) * scale
    lower = scaled.to_integral_value(rounding=ROUND_FLOOR)
    n = lower + 1 if scaled - lower >= Decimal("0.5") else lower
    return float(n / scale)


def matrix_numbers(value: float) -> list[float]:
    """The numbers X's script reads back from a printed computed-matrix entry.

    Chromium prints six significant digits (C's %.6g, scientific below 1e-4) and the script splits the text on
    /[\\d.-]+/g, so "5.68575e-05" gives two numbers: 5.68575 and -5.
    """
    text = "0" if value == 0 else format(value, ".6g")
    return [float(x) for x in re.findall(r"[\d.-]+", text)]


def interpolate(from_list: list[float], to_list: list[float], f: float):
    if len(from_list) != len(to_list):
        raise ValueError(f"Mismatched interpolation args {from_list}: {to_list}")
    return [a * (1 - f) + b * f for a, b in zip(from_list, to_list)]


def get_rotation_matrix(rotation: float):
    rad = math.radians(rotation)
    return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]


def solve(value: float, min_val: float, max_val: float, rounding: bool):
    result = value * (max_val - min_val) / 255 + min_val
    return math.floor(result) if rounding else js_round(result * 100) / 100


def float_to_hex(x):
    # todo: ?
    result = []
    quotient = int(x)
    fraction = x - quotient

    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (float(quotient) * 16))

        if remainder > 9:
            result.insert(0, chr(remainder + 55))
        else:
            result.insert(0, str(remainder))

        x = float(quotient)

    if fraction == 0:
        return "".join(result)

    result.append(".")

    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)

        if integer > 9:
            result.append(chr(integer + 55))
        else:
            result.append(str(integer))

    return "".join(result)


def cacl_anim_key(frames: list[float], target_time: float) -> str:
    """Colour and rotation matrix of X's loading animation at `target_time`, printed the way X's script reads
    them from the browser (Lqm1/x-client-transaction-id#26, checked against Chromium-recorded vectors)."""
    from_color, to_color = frames[:3], frames[3:6]
    to_rotation = solve(frames[6], 60.0, 360.0, True)
    curves = [solve(x, -1.0 if i % 2 else 0.0, 1.0, False) for i, x in enumerate(frames[7:])]
    val = Cubic(curves).get_value(target_time)

    # The eased value can overshoot [0, 1]; the browser clamps colour channels to 0..255.
    color = [
        format(js_round(max(0.0, min(255.0, channel))), "x")
        for channel in interpolate(from_color, to_color, val)
    ]
    rotation = interpolate([0.0], [to_rotation], val)[0]
    matrix = []
    for value in get_rotation_matrix(rotation):
        for number in matrix_numbers(value):
            hex_value = float_to_hex(abs(js_to_fixed(number, 2))).lower()
            matrix.append(f"0{hex_value}" if hex_value.startswith(".") else hex_value or "0")

    return re.sub(r"[.-]", "", "".join([*color, *matrix, "0", "0"]))


def animation_key(vk_bytes: list[int], anim_idx: list[int], frame_rows: list[list[float]]) -> str:
    """The animation key for a verification key: the row and time its key bytes select."""
    frame_time = 1
    for x in anim_idx[1:]:
        frame_time *= vk_bytes[x] % 16
    frame_row = frame_rows[vk_bytes[anim_idx[0]] % 16]
    return cacl_anim_key(frame_row, js_round(frame_time / 10) * 10 / 4096)


def parse_vk_bytes(soup: bs4.BeautifulSoup) -> list[int]:
    el = soup.find("meta", {"name": "twitter-site-verification", "content": True})
    el = str(el.get("content")) if el and isinstance(el, bs4.Tag) else None
    if not el:
        raise XClIdParseError("Couldn't get XClientTxId key bytes")

    return list(base64.b64decode(bytes(el, "utf-8")))


# Hashed x-web builds (e.g. `assets/Cm5_4wZ7.js`) have no named reference: the signing chunk is a lazily loaded
# module whose default export is called, a few static imports below the page entry (vladkens/twscrape#379, #380).
STATIC_IMPORT_RE = re.compile(r"""\b(?:from|import)\s*(["'`])([\w./-]+\.js)\1""")
DEFAULT_IMPORT_RE = re.compile(
    r"""\bimport\(\s*(["'`])([\w./-]+\.js)\1\s*\)\.then\(\s*\(?(\w+)\)?\s*=>\s*\3\.default\(\)"""
)
MAX_IMPORT_DEPTH = 2  # static import hops followed from the page scripts
MAX_IMPORTED_ASSETS = 32  # chunks fetched beyond the page scripts
# The signing chunk carries several `(a[N], 16)` indices; an unrelated large chunk can match once.
MIN_SIGNING_INDICES = 2


def _signing_indices(script_text: str) -> list[int] | None:
    items = [int(x.group(2)) for x in INDICES_REGEX.finditer(script_text)]
    return items if len(items) >= MIN_SIGNING_INDICES else None


async def parse_anim_idx(text: str, clt: httpx.AsyncClient | None = None) -> list[int]:
    scripts = list(get_scripts_list(text))
    preferred_scripts = [x for x in scripts if INDICES_FILE_RE.search(x)]
    fallback_scripts = [x for x in scripts if x not in preferred_scripts]
    scripts = [x for x in preferred_scripts + fallback_scripts if _is_allowed_script_url(x)]
    if not scripts:
        raise XClIdParseError("Couldn't get XClientTxId scripts")

    last_error: Exception | None = None
    seen, level, budget = set(scripts), scripts, MAX_IMPORTED_ASSETS

    async def fetch_indices(url: str) -> list[int] | None:
        nonlocal last_error
        try:
            return _signing_indices(await get_tw_page_text(url, clt=clt))
        except Exception as e:
            last_error = e
            return None

    for _depth in range(MAX_IMPORT_DEPTH + 1):
        static_imports: list[str] = []
        default_imports: list[str] = []
        for script_url in level:
            try:
                script_text = await get_tw_page_text(script_url, clt=clt)
            except Exception as e:
                last_error = e
                continue

            items = _signing_indices(script_text)
            if items:
                return items

            named = INDICES_FILE_RE.search(script_text)
            if named:
                indices_url = urljoin(script_url, named.group(0))
                if _is_allowed_script_url(indices_url):
                    items = await fetch_indices(indices_url)
                    if items:
                        return items

            static_imports += [
                urljoin(script_url, m.group(2)) for m in STATIC_IMPORT_RE.finditer(script_text)
            ]
            default_imports += [
                urljoin(script_url, m.group(2)) for m in DEFAULT_IMPORT_RE.finditer(script_text)
            ]

        for url in dict.fromkeys(default_imports):
            if url in seen or not _is_allowed_script_url(url) or budget <= 0:
                continue
            seen.add(url)
            budget -= 1
            items = await fetch_indices(url)
            if items:
                return items

        level = [
            u for u in dict.fromkeys(static_imports) if u not in seen and _is_allowed_script_url(u)
        ]
        level = level[: max(0, budget)]
        seen.update(level)
        budget -= len(level)
        if not level:
            break

    raise XClIdParseError("Couldn't get XClientTxId indices") from last_error


def parse_anim_arr(soup: bs4.BeautifulSoup, vk_bytes: list[int]) -> list[list[float]]:
    # https://github.com/fa0311/twitter-tid-deobf/blob/c4fd61c36/output/a.js#L18
    els = list(soup.select("svg[id^='loading-x-anim'] g:first-child path:nth-child(2)"))
    els = [str(x.get("d") or "").strip() for x in els]
    if not els:
        raise XClIdParseError("Couldn't get XClientTxId animation array")

    idx = vk_bytes[5] % len(els)
    dat = els[idx][9:].split("C")
    arr = [list(map(float, re.sub(r"[^\d]+", " ", x).split())) for x in dat]
    return arr


async def load_keys(
    page_text: str, soup: bs4.BeautifulSoup, clt: httpx.AsyncClient | None = None
) -> tuple[list[int], str]:
    anim_idx = await parse_anim_idx(page_text, clt=clt)
    vk_bytes = parse_vk_bytes(soup)
    anim_arr = parse_anim_arr(soup, vk_bytes)

    anim_key = animation_key(vk_bytes, anim_idx, anim_arr)
    return vk_bytes, anim_key


class XClIdGen:
    @staticmethod
    async def create(
        clt: httpx.AsyncClient | None = None,
        proxy: str | None = None,
        user_agent: str | None = None,
        cookies: dict[str, str] | None = None,
        impersonate: str | None = None,
    ) -> "XClIdGen":
        owns_client = clt is None
        clt = clt or _make_client(
            proxy=proxy, user_agent=user_agent, cookies=cookies, impersonate=impersonate
        )
        last_error: Exception | None = None

        try:
            for _ in range(3):
                try:
                    text = await get_tw_page_text("https://x.com/tesla", clt=clt)
                    soup = bs4.BeautifulSoup(text, "html.parser")
                    vk_bytes, anim_key = await load_keys(text, soup, clt=clt)
                    return XClIdGen(vk_bytes, anim_key)
                except XClIdAccountError:
                    raise
                except (httpx.HTTPError, XClIdError) as e:
                    last_error = e
                    await asyncio.sleep(1)
        finally:
            if owns_client:
                await clt.aclose()

        if isinstance(last_error, XClIdParseError):
            raise XClIdParseError(
                "Couldn't initialize XClient transaction id generator"
            ) from last_error
        raise XClIdError("Couldn't initialize XClient transaction id generator") from last_error

    def __init__(self, vk_bytes: list[int], anim_key: str):
        self.vk_bytes = vk_bytes
        self.anim_key = anim_key

    def calc(self, method: str, path: str) -> str:
        ts = math.floor((time.time() * 1000 - 1682924400 * 1000) / 1000)
        ts_bytes = [(ts >> (i * 8)) & 0xFF for i in range(4)]

        dkw, drn = "obfiowerehiring", 3  # default keyword and random number
        pld = f"{method.upper()}!{path}!{ts}{dkw}{self.anim_key}"
        pld = list(hashlib.sha256(pld.encode()).digest())
        pld = [*self.vk_bytes, *ts_bytes, *pld[:16], drn]

        num = random.randint(0, 255)
        pld = bytearray([num, *[x ^ num for x in pld]])
        out = base64.b64encode(pld).decode("utf-8").strip("=")
        return out


# MARK: Demo code


async def main():
    text = await get_tw_page_text("https://x.com/elonmusk")
    soup = bs4.BeautifulSoup(text, "html.parser")

    vk_bytes, anim_key = await load_keys(text, soup)
    clid_gen = XClIdGen(vk_bytes, anim_key)

    method = "GET"
    path = "/i/api/graphql/AIdc203rPpK_k_2KWSdm7g/SearchTimeline"
    clid = clid_gen.calc(method, path)
    print(clid)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
