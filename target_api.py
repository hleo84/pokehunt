import asyncio
import logging
import random
import uuid
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Target's public frontend API key (embedded in target.com JS bundles)
TARGET_API_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"

SEARCH_URL = "https://api.target.com/products/v3/product_summaries"
REDSKY_URL = (
    "https://redsky.target.com/redsky_aggregations/v1/web"
    "/product_summary_with_fulfillment_v1"
)

# Two complementary queries — different enough to surface different listings
SEARCH_TERMS = [
    "pokemon trading card game",
    "pokemon booster",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


def _random_headers() -> dict:
    ua = random.choice(USER_AGENTS)
    is_chrome = "Chrome" in ua
    return {
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.target.com",
        "Referer": "https://www.target.com/c/trading-card-games/-/N-5xtg6",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"' if is_chrome else "",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Connection": "keep-alive",
    }


def _fresh_visitor_id() -> str:
    return uuid.uuid4().hex[:16].upper()


class TargetAPIClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_json(self, url: str, params: dict, retries: int = 3) -> Optional[dict]:
        session = await self._session_get()
        for attempt in range(retries):
            try:
                async with session.get(url, params=params, headers=_random_headers()) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    if resp.status == 429:
                        wait = 2 ** attempt * 10 + random.uniform(0, 5)
                        logger.warning("Rate limited — waiting %.1fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    logger.warning("HTTP %s from %s", resp.status, url)
                    return None
            except aiohttp.ClientError as exc:
                logger.warning("Request error (attempt %d): %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)
        return None

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
            "visitor_id": _fresh_visitor_id(),
        }
        data = await self._get_json(SEARCH_URL, params)
        if not data:
            return []
        results = _parse_search(data)
        if not results:
            total = data.get("products", {}).get("total_results", "unknown")
            logger.warning("Search for %r returned 0 parsed products (API total_results=%s)", term, total)
        return results

    async def search_all_pokemon_tcg(self, count: int = 24) -> list[dict]:
        """Run both search terms in parallel and deduplicate by TCIN."""
        results = await asyncio.gather(
            *[self._search_one_term(term, count) for term in SEARCH_TERMS]
        )
        seen: dict[str, dict] = {}
        for batch in results:
            for product in batch:
                seen.setdefault(product["tcin"], product)
        return list(seen.values())

    async def check_tcins(self, tcins: list[str]) -> list[dict]:
        """Fetch live fulfillment data for a list of specific TCINs."""
        if not tcins:
            return []
        params = {
            "tcin": ",".join(tcins),
            "store_id": "2307",
            "zip": "91506",
            "state": "CA",
            "latitude": "34.170",
            "longitude": "-118.270",
            "scheduled_delivery_store_id": "2307",
            "paid_membership": "false",
            "base_membership": "false",
            "card_membership": "false",
            "required_store_id": "3991",
            "country": "US",
            "channel": "WEB",
            "page": "/s/pokemon",
            "key": TARGET_API_KEY,
            "visitor_id": _fresh_visitor_id(),
        }
        data = await self._get_json(REDSKY_URL, params)
        return _parse_redsky(data) if data else []


# ---------------------------------------------------------------------------
# Parsers — isolated so they're easy to update when Target tweaks its schema
# ---------------------------------------------------------------------------

def _parse_search(data: dict) -> list[dict]:
    results = []
    for item in data.get("products", {}).get("items", []):
        product = item.get("product", {})
        tcin = product.get("tcin", "")
        if not tcin:
            continue

        desc = product.get("item", {}).get("product_description", {})
        title = desc.get("title", "Unknown Product")

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
            "price": f"${price:.2f}" if price else "N/A",
            "url": f"https://www.target.com/p/-/A-{tcin}",
        })
    return results


def _parse_redsky(data: dict) -> list[dict]:
    results = []
    for item in data.get("data", {}).get("product_summaries", []):
        tcin = item.get("tcin", "")
        if not tcin:
            continue

        title = (
            item.get("item", {})
            .get("product_description", {})
            .get("title", "Unknown Product")
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
            "price": f"${price:.2f}" if price else "N/A",
            "url": f"https://www.target.com/p/-/A-{tcin}",
        })
    return results
