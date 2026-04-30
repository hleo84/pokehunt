import asyncio
import logging
import os
import random
import re
import uuid
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

TARGET_API_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"

SEARCH_URL = "https://api.target.com/products/v3/product_summaries"
REDSKY_URL = (
    "https://redsky.target.com/redsky_aggregations/v1/web"
    "/product_summary_with_fulfillment_v1"
)

# Editable in Railway Variables as a comma-separated list.
# e.g. SEARCH_TERMS=pokemon trading card game,pokemon booster,pokemon elite trainer box
_DEFAULT_SEARCH_TERMS = [
    "pokemon trading card game",
    "pokemon booster",
]
SEARCH_TERMS: list[str] = [
    t.strip()
    for t in os.getenv("SEARCH_TERMS", "").split(",")
    if t.strip()
] or _DEFAULT_SEARCH_TERMS

# ---------------------------------------------------------------------------
# Cookie management — updated at runtime by Playwright refresh
# ---------------------------------------------------------------------------
_raw_cookies: str = os.getenv("TARGET_COOKIES", "")
_cached_visitor_id: str = ""


def _extract_visitor_id(cookie_str: str) -> str:
    m = re.search(r"visitorId=([A-Fa-f0-9]+)", cookie_str)
    return m.group(1).upper() if m else ""


_cached_visitor_id = _extract_visitor_id(_raw_cookies)
if _raw_cookies:
    logger.info("TARGET_COOKIES pre-loaded from env (%d chars)", len(_raw_cookies))


def update_cookies(cookie_str: str) -> None:
    """Replace active cookies at runtime (called after Playwright refresh)."""
    global _raw_cookies, _cached_visitor_id
    _raw_cookies = cookie_str
    _cached_visitor_id = _extract_visitor_id(cookie_str)
    logger.info(
        "Cookies updated — %d chars, visitorId=%s",
        len(cookie_str), _cached_visitor_id or "(not found)",
    )


def current_cookies() -> str:
    return _raw_cookies


def _visitor_id() -> str:
    return _cached_visitor_id or uuid.uuid4().hex.upper()


class CookiesExpiredError(Exception):
    """Raised when Target returns HTTP 403 — Playwright refresh needed."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _build_headers() -> dict:
    ua = random.choice(USER_AGENTS)
    is_mobile = "iPhone" in ua
    h = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.target.com",
        "Referer": "https://www.target.com/s?searchTerm=pokemon",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?1" if is_mobile else "?0",
        "sec-ch-ua-platform": '"iOS"' if is_mobile else '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "priority": "u=1, i",
    }
    if _raw_cookies:
        h["Cookie"] = _raw_cookies
    return h


def _product_image_url(tcin: str, api_url: str = "") -> str:
    """API-provided URL preferred; fall back to Target's scene7 CDN."""
    if api_url and api_url.startswith("http"):
        return api_url
    return f"https://target.scene7.com/is/image/Target/{tcin}?wid=400&hei=400&qlt=80&fmt=webp"


def _cookie_str_to_playwright(cookie_str: str) -> list[dict]:
    """Convert a raw cookie header string into Playwright's cookie dict format."""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        for domain in (".target.com", "www.target.com", "redsky.target.com"):
            cookies.append({"name": name.strip(), "value": value, "domain": domain, "path": "/"})
    return cookies


# Third-party domains blocked in Playwright — analytics, ads, tracking.
# Target's own domains (target.com, redsky.target.com) are always allowed.
_BLOCKED_DOMAINS = {
    "google-analytics.com", "googletagmanager.com", "googlesyndication.com",
    "doubleclick.net", "googleadservices.com",
    "omtrdc.net", "demdex.net", "adobe.com", "adobedtm.com",
    "mparticle.com", "segment.io", "segment.com",
    "facebook.com", "facebook.net", "fbcdn.net",
    "twitter.com", "t.co",
    "quantserve.com", "scorecardresearch.com",
    "perimeterx.net",   # PX reporting (cookies already set — don't need callbacks)
    "akstat.io", "akamaiedge.net",
}

# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class TargetAPIClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._playwright = None
        self._browser = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def _get_browser(self):
        """Return a persistent Chromium instance, launching one if needed."""
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        # Start fresh (first call or after a crash)
        if self._playwright is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--disable-extensions",
                "--disable-background-networking",
                "--blink-settings=imagesEnabled=false",  # disable images at engine level
            ],
        )
        logger.info("Chromium launched (persistent — stays alive between polls)")
        return self._browser

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._browser and self._browser.is_connected():
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _get_json(self, url: str, params: dict, retries: int = 3) -> Optional[dict]:
        session = await self._get_session()
        for attempt in range(retries):
            try:
                async with session.get(url, params=params, headers=_build_headers()) as resp:
                    if resp.status in (200, 206):
                        return await resp.json(content_type=None)
                    if resp.status == 403:
                        raise CookiesExpiredError(
                            "HTTP 403 — PerimeterX blocked; triggering cookie refresh"
                        )
                    if resp.status == 429:
                        wait = 2 ** attempt * 10 + random.uniform(0, 5)
                        logger.warning("Rate limited — waiting %.1fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    logger.warning("HTTP %s from %s", resp.status, url)
                    return None
            except CookiesExpiredError:
                raise
            except aiohttp.ClientError as exc:
                logger.warning("Request error (attempt %d): %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)
        return None

    async def playwright_search(self, terms: list[str]) -> list[dict]:
        """
        Load Target's search page in Playwright and intercept whatever product
        API call Target's own frontend makes. Automatically adapts if Target
        ever changes their endpoint.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed")
            return []

        seen: dict[str, dict] = {}

        browser = await self._get_browser()

        # Run terms sequentially — parallel contexts crash on low memory
        for term in terms:
            ctx = None
            try:
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/18.5 Mobile/15E148 Safari/604.1"
                    ),
                    viewport={"width": 390, "height": 844},
                    locale="en-US",
                )
                if _raw_cookies:
                    await ctx.add_cookies(_cookie_str_to_playwright(_raw_cookies))

                page = await ctx.new_page()

                async def _should_block(route):
                    url = route.request.url
                    # Block by file extension
                    if any(url.lower().endswith(ext) for ext in (
                        ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                        ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot",
                        ".mp4", ".mp3", ".avi", ".mov",
                    )):
                        await route.abort()
                        return
                    # Block third-party analytics / ad domains
                    if any(d in url for d in _BLOCKED_DOMAINS):
                        await route.abort()
                        return
                    await route.continue_()

                await page.route("**/*", _should_block)

                captured: list[tuple[str, dict]] = []
                got_data = asyncio.Event()

                async def handle_response(response, _captured=captured):
                    url = response.url
                    if (
                        response.status in (200, 206)
                        and ("redsky.target.com" in url or "api.target.com" in url)
                        and any(k in url for k in ("product_summar", "plp_search_v"))
                    ):
                        try:
                            data = await response.json()
                            _captured.append((url, data))
                            logger.info("Intercepted: %s", url.split("?")[0])
                            got_data.set()  # signal — close page early
                        except Exception:
                            pass

                page.on("response", handle_response)

                search_url = f"https://www.target.com/s?searchTerm={term.replace(' ', '+')}"
                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:
                    logger.warning("page.goto failed for %r: %s", term, exc)
                    await ctx.close()
                    continue

                # Wait up to 8s for data, but close immediately once we have it
                try:
                    await asyncio.wait_for(got_data.wait(), timeout=8)
                except asyncio.TimeoutError:
                    logger.warning("Search %r — no product API response intercepted", term)

                # Close page right away — no need to wait for full render
                await ctx.close()
                ctx = None

                for endpoint_url, data in captured:
                    results = _parse_search(data) or _parse_redsky(data)
                    for p in results:
                        seen.setdefault(p["tcin"], p)
                    if results:
                        logger.info(
                            "Search %r → %d products via %s",
                            term, len(results), endpoint_url.split("?")[0],
                        )

            except Exception:
                logger.exception("Error searching term %r", term)
            finally:
                if ctx:
                    await ctx.close()

        logger.info("playwright_search: %d unique products across %d terms", len(seen), len(terms))
        return list(seen.values())

    # Legacy direct API search — kept for reference (returns 404 as of 2026)
    # async def search_all_pokemon_tcg(self, count: int = 40) -> list[dict]: ...

    # ------------------------------------------------------------------
    # TCIN mode — disabled by default, kept for easy re-enablement
    # Uncomment the call in bot.py _fetch_products() to activate.
    # ------------------------------------------------------------------
    async def check_tcins(self, tcins: list[str]) -> list[dict]:
        """Direct TCIN lookup (faster, but misses unknown products)."""
        if not tcins:
            return []
        params = {
            "tcins": ",".join(tcins),
            "store_id": "2307",
            "zip": "91202",
            "state": "CA",
            "latitude": "34.170",
            "longitude": "-118.270",
            "scheduled_delivery_store_id": "2307",
            "paid_membership": "false",
            "base_membership": "false",
            "card_membership": "false",
            "required_store_id": "2307",
            "country": "US",
            "channel": "WEB",
            "page": "/s/pokemon",
            "key": TARGET_API_KEY,
            "visitor_id": _visitor_id(),
        }
        data = await self._get_json(REDSKY_URL, params)
        if not data:
            return []
        return _parse_redsky(data)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_search(data: dict) -> list[dict]:
    results = []
    for item in data.get("products", {}).get("items", []):
        product = item.get("product", {})
        tcin = product.get("tcin", "")
        if not tcin:
            continue

        title = (
            product.get("item", {})
            .get("product_description", {})
            .get("title", "Unknown Product")
        )
        api_img = (
            product.get("item", {})
            .get("enrichment", {})
            .get("images", {})
            .get("primary_image_url", "")
        )
        ship_status = (
            item.get("fulfillment", {})
            .get("shipping_options", {})
            .get("availability_status", "UNKNOWN")
        )
        price = item.get("pricing", {}).get("current_retail")

        results.append({
            "tcin": tcin,
            "title": title,
            "ship_status": ship_status,
            "quantity": None,
            "price": f"${price:.2f}" if price else "N/A",
            "image": _product_image_url(tcin, api_img),
            "url": f"https://www.target.com/p/-/A-{tcin}",
        })
    return results


def _parse_redsky(data: dict) -> list[dict]:
    results = []
    raw_items = data.get("data", {}).get("product_summaries", [])
    logger.info("_parse_redsky: %d items", len(raw_items))

    if not raw_items and isinstance(data, dict):
        for k, v in data.items():
            logger.info("  top-level key %r → %s", k, type(v).__name__)

    skipped = 0
    for item in raw_items:
        tcin = item.get("tcin", "")
        if not tcin:
            continue

        title = (
            item.get("item", {})
            .get("product_description", {})
            .get("title", "Unknown Product")
        )
        api_img = (
            item.get("item", {})
            .get("enrichment", {})
            .get("images", {})
            .get("primary_image_url", "")
        )

        shipping = item.get("fulfillment", {}).get("shipping_options", {})
        ship_status = shipping.get("availability_status", "UNKNOWN")
        qty = (
            shipping.get("available_to_promise_quantity")
            or shipping.get("quantity")
        )

        pricing = item.get("price") or item.get("pricing") or {}
        price_type = pricing.get("formatted_current_price_type", "")
        current_retail = pricing.get("current_retail")
        reg_retail = pricing.get("reg_retail")
        formatted_price = pricing.get("formatted_current_price", "N/A")

        # Keep: MSRP ("reg") or priced within $5 above MSRP
        if price_type and price_type != "reg":
            if reg_retail is not None and current_retail is not None:
                if current_retail > reg_retail + 5:
                    skipped += 1
                    continue
            else:
                skipped += 1
                continue

        price_str = f"${current_retail:.2f}" if current_retail else formatted_price

        results.append({
            "tcin": tcin,
            "title": title,
            "ship_status": ship_status,
            "quantity": qty,
            "price": price_str,
            "image": _product_image_url(tcin, api_img),
            "url": f"https://www.target.com/p/-/A-{tcin}",
        })

    if skipped:
        logger.info("_parse_redsky: skipped %d non-MSRP items", skipped)
    return results
