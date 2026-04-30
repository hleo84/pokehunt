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


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class TargetAPIClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

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

    async def search_all_pokemon_tcg(self, count: int = 40) -> list[dict]:
        """Keyword search across two terms, deduplicated by TCIN."""
        batches = await asyncio.gather(
            *[self._search_one_term(t, count) for t in SEARCH_TERMS]
        )
        seen: dict[str, dict] = {}
        for batch in batches:
            for p in batch:
                seen.setdefault(p["tcin"], p)
        return list(seen.values())

    async def _search_one_term(self, term: str, count: int) -> list[dict]:
        params = {
            "search_term": term,
            "channel": "WEB",
            "count": count,
            "default_purchasability_filter": "false",
            "include_list": "facets,promotions",
            "offset": 0,
            "platform": "desktop",
            "key": TARGET_API_KEY,
            "visitor_id": _visitor_id(),
        }
        data = await self._get_json(SEARCH_URL, params)
        if not data:
            return []
        results = _parse_search(data)
        if not results:
            total = data.get("products", {}).get("total_results", "unknown")
            logger.warning("Search %r → 0 products (API total=%s)", term, total)
        return results

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
