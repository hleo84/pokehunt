import asyncio
import logging
import os
import random
import re
import uuid
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Target's public frontend API key (embedded in target.com JS bundles)
TARGET_API_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"

REDSKY_URL = (
    "https://redsky.target.com/redsky_aggregations/v1/web"
    "/product_summary_with_fulfillment_v1"
)

# Paste your browser cookie string here via the TARGET_COOKIES env var.
# Refresh it every 1-2 days when you start seeing 403s again.
_RAW_COOKIES: str = os.getenv("TARGET_COOKIES", "")

# Pull the visitorId out of the cookie string so the URL param matches.
_VISITOR_ID_MATCH = re.search(r"visitorId=([A-F0-9]+)", _RAW_COOKIES)
_COOKIE_VISITOR_ID: str = _VISITOR_ID_MATCH.group(1) if _VISITOR_ID_MATCH else ""

if _RAW_COOKIES:
    logger.info("TARGET_COOKIES loaded (%d chars)", len(_RAW_COOKIES))
    if _COOKIE_VISITOR_ID:
        logger.info("visitorId from cookies: %s", _COOKIE_VISITOR_ID)
else:
    logger.warning(
        "TARGET_COOKIES not set — requests will likely 403. "
        "Copy the cookie header from your browser's DevTools and set it in Railway."
    )

USER_AGENTS = [
    # Match the iPhone UA seen in DevTools (Target's PerimeterX is UA-sensitive)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _build_headers(visitor_id: str) -> dict:
    ua = random.choice(USER_AGENTS)
    is_mobile = "iPhone" in ua
    headers = {
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
    if _RAW_COOKIES:
        headers["Cookie"] = _RAW_COOKIES
    return headers


def _visitor_id() -> str:
    """Return the visitorId from cookies (preferred) or generate a fresh one."""
    return _COOKIE_VISITOR_ID or uuid.uuid4().hex.upper()


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
        vid = _visitor_id()
        for attempt in range(retries):
            try:
                async with session.get(
                    url, params=params, headers=_build_headers(vid)
                ) as resp:
                    if resp.status in (200, 206):
                        return await resp.json(content_type=None)
                    if resp.status == 403:
                        logger.error(
                            "HTTP 403 — PerimeterX blocked the request. "
                            "Refresh TARGET_COOKIES in Railway env vars with a fresh "
                            "cookie string from your browser's DevTools."
                        )
                        return None
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

    async def check_tcins(self, tcins: list[str]) -> list[dict]:
        """Fetch live fulfillment data for a list of specific TCINs."""
        if not tcins:
            return []
        vid = _visitor_id()
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
            "visitor_id": vid,
        }
        data = await self._get_json(REDSKY_URL, params)
        if not data:
            logger.warning("check_tcins: no data returned from redsky")
            return []
        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        logger.info("check_tcins raw response keys: %s", keys)
        results = _parse_redsky(data)
        if not results:
            logger.warning("check_tcins: parsed 0 products, raw sample: %s", str(data)[:2000])
        return results


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_redsky(data: dict) -> list[dict]:
    results = []
    raw_items = data.get("data", {}).get("product_summaries", [])
    logger.info("_parse_redsky: %d raw items at data.product_summaries", len(raw_items))

    if not raw_items and isinstance(data, dict):
        for k, v in data.items():
            logger.info(
                "  top-level key %r → %s (len=%s)",
                k, type(v).__name__, len(v) if hasattr(v, "__len__") else "n/a",
            )

    skipped_price = 0
    for item in raw_items:
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

        pricing = item.get("price") or item.get("pricing") or {}
        price_type = pricing.get("formatted_current_price_type", "")
        current_retail = pricing.get("current_retail")
        reg_retail = pricing.get("reg_retail")
        formatted_price = pricing.get("formatted_current_price", "N/A")

        # Keep items at MSRP ("reg") or priced within $5 above MSRP.
        # Skip deep discounts (clearance/sale more than $5 off).
        if price_type and price_type != "reg":
            # We have a non-reg price — allow it only if it's ≤ reg + $5
            if reg_retail is not None and current_retail is not None:
                if current_retail > reg_retail + 5:
                    logger.debug(
                        "Skipping TCIN %s — price_type=%r, current=%.2f, reg=%.2f",
                        tcin, price_type, current_retail, reg_retail,
                    )
                    skipped_price += 1
                    continue
                # else: within $5 of MSRP — allow it through
            else:
                # No reg_retail to compare against; skip anything non-reg
                logger.debug("Skipping TCIN %s — price_type=%r (no reg_retail)", tcin, price_type)
                skipped_price += 1
                continue

        price_str = f"${current_retail:.2f}" if current_retail else formatted_price

        results.append({
            "tcin": tcin,
            "title": title,
            "ship_status": ship_status,
            "price": price_str,
            "url": f"https://www.target.com/p/-/A-{tcin}",
        })

    if skipped_price:
        logger.info("_parse_redsky: skipped %d items (non-reg price_type)", skipped_price)
    return results
