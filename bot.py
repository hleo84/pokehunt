"""
Pokemon TCG Target Restock Bot
- Polls Target's API every ~3 minutes (paused 2–7 AM PST)
- Cookies refreshed automatically via Playwright (no manual work)
- State persists in Redis so restarts don't cause duplicate alerts
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
import redis.asyncio as aioredis
from discord.ext import commands, tasks
from dotenv import load_dotenv

import target_api
from target_api import CookiesExpiredError, TargetAPIClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DISCORD_TOKEN   = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID      = int(os.environ["DISCORD_CHANNEL_ID"])
NOTIFY_ROLE_ID  = os.getenv("DISCORD_ROLE_ID", "")
POLL_SECONDS    = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))
JITTER_SECONDS  = int(os.getenv("POLL_JITTER_SECONDS", "45"))
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379")

REDIS_STATE_KEY   = "pokemon_tcg_stock_state"
REDIS_COOKIES_KEY = "target_cookies"
COOKIE_REFRESH_HOURS = 20  # refresh before PerimeterX tokens expire (~24-48h)

# Quiet hours — no polls, no alerts (America/Los_Angeles = PST/PDT auto)
QUIET_START_HOUR = 2   # 2 AM
QUIET_END_HOUR   = 7   # 7 AM
PST = ZoneInfo("America/Los_Angeles")

# WATCH_TCINS kept for easy re-enablement — not active in default mode
WATCH_TCINS: list[str] = [
    t.strip() for t in os.getenv("WATCH_TCINS", "").split(",") if t.strip()
]

IN_STOCK_STATUSES = {"IN_STOCK", "LIMITED_STOCK"}

# ---------------------------------------------------------------------------
# Bot + globals
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

api_client: TargetAPIClient
redis_client: aioredis.Redis
stock_state: dict[str, dict] = {}
_seeded               = False
_startup_done         = False  # guard against duplicate init on reconnect
_cookie_refresh_count = 0      # skip first immediate fire of cookie_refresh_task


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------
async def _load_state() -> dict:
    try:
        raw = await redis_client.get(REDIS_STATE_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        logger.exception("Failed to load state from Redis")
        return {}


async def _save_state():
    try:
        await redis_client.set(REDIS_STATE_KEY, json.dumps(stock_state))
    except Exception:
        logger.exception("Failed to save state to Redis")


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------
def _quiet_hours_remaining() -> float | None:
    """Return seconds until 7 AM PST if we're in quiet hours, else None."""
    now = datetime.now(PST)
    if QUIET_START_HOUR <= now.hour < QUIET_END_HOUR:
        wake = now.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
        return (wake - now).total_seconds()
    return None


# ---------------------------------------------------------------------------
# Cookie management (Playwright)
# ---------------------------------------------------------------------------
async def _playwright_get_cookies() -> str:
    """Launch headless Chromium, browse Target, return a fresh cookie string."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed — check requirements.txt")
        return ""

    logger.info("Playwright: launching Chromium (~20s)…")
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/18.5 Mobile/15E148 Safari/604.1"
                ),
                viewport={"width": 390, "height": 844},
                locale="en-US",
            )
            page = await ctx.new_page()

            await page.goto(
                "https://www.target.com/s?searchTerm=pokemon",
                wait_until="networkidle",
                timeout=60_000,
            )
            # Extra wait for PerimeterX sensor to fully initialize
            await asyncio.sleep(5)

            cookies = await ctx.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            await browser.close()

            px_ok = "_px2" in cookie_str
            logger.info(
                "Playwright: %d cookies (%d chars), _px2=%s",
                len(cookies), len(cookie_str), px_ok,
            )
            return cookie_str

    except Exception:
        logger.exception("Playwright cookie refresh failed")
        return ""


async def _refresh_cookies(notify_channel: discord.TextChannel | None = None) -> bool:
    """Run Playwright, push cookies to target_api, persist to Redis."""
    if notify_channel:
        await notify_channel.send("🔄 Refreshing Target cookies via Playwright (~20s)…")

    cookie_str = await _playwright_get_cookies()
    if not cookie_str:
        logger.error("Cookie refresh returned empty string — keeping old cookies")
        if notify_channel:
            await notify_channel.send("❌ Cookie refresh failed — check Railway logs.")
        return False

    target_api.update_cookies(cookie_str)
    try:
        await redis_client.setex(REDIS_COOKIES_KEY, COOKIE_REFRESH_HOURS * 3600, cookie_str)
    except Exception:
        logger.exception("Failed to persist cookies to Redis")

    if notify_channel:
        await notify_channel.send("✅ Cookies refreshed successfully.")
    return True


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------
def _role_mention() -> str:
    return f"<@&{NOTIFY_ROLE_ID}>" if NOTIFY_ROLE_ID else "@here"


def _spotted_embed(product: dict) -> discord.Embed:
    embed = discord.Embed(
        title="👀 New Pokemon TCG Product Spotted",
        description=(
            f"**{product['title']}**\n"
            "Appeared in Target's system — not yet purchasable."
        ),
        color=0xFFD700,
        url=product["url"],
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Status", value=product["ship_status"], inline=True)
    embed.add_field(name="Price",  value=product["price"],       inline=True)
    embed.add_field(name="Link",   value=f"[target.com]({product['url']})", inline=False)
    if product.get("image"):
        embed.set_thumbnail(url=product["image"])
    embed.set_footer(text=f"TCIN {product['tcin']} • Target Monitor")
    return embed


def _restock_embed(product: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🎴 Pokemon TCG — Now Available Online!",
        description=f"**{product['title']}**",
        color=0xCC0000,
        url=product["url"],
        timestamp=datetime.now(timezone.utc),
    )
    status_val = product["ship_status"]
    if product.get("quantity") is not None:
        status_val += f"  ({product['quantity']} units)"
    embed.add_field(name="🚚 Online Status", value=status_val,                        inline=True)
    embed.add_field(name="💰 Price",         value=product["price"],                  inline=True)
    embed.add_field(name="🔗 Buy Now",       value=f"[target.com]({product['url']})", inline=False)
    if product.get("image"):
        embed.set_thumbnail(url=product["image"])
    embed.set_footer(text=f"TCIN {product['tcin']} • Target Monitor")
    return embed


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def _seed_baseline(products: list[dict]):
    """Record all current products silently — no alerts on startup."""
    for p in products:
        stock_state[p["tcin"]] = {
            "ship": p["ship_status"] in IN_STOCK_STATUSES,
            "ever_seen": True,
        }
    logger.info("Seeded baseline: %d products, no alerts sent", len(products))


async def _process_products(channel: discord.TextChannel, products: list[dict]):
    for p in products:
        tcin     = p["tcin"]
        prev     = stock_state.get(tcin, {"ship": False, "ever_seen": False})
        ship_now = p["ship_status"] in IN_STOCK_STATUSES

        # Stage 1 — new product appeared in Target's system
        if not prev["ever_seen"]:
            logger.info("SPOTTED: %s (TCIN %s) status=%s", p["title"], tcin, p["ship_status"])
            await channel.send(embed=_spotted_embed(p))

        # Stage 2 — product went purchasable online
        if ship_now and not prev["ship"]:
            logger.info("IN STOCK: %s (TCIN %s)", p["title"], tcin)
            await channel.send(content=_role_mention(), embed=_restock_embed(p))

        stock_state[tcin] = {"ship": ship_now, "ever_seen": True}


async def _fetch_products() -> list[dict]:
    # Keyword search — discovers any Pokemon TCG product automatically.
    # To re-enable TCIN mode, comment the return and uncomment below:
    # if WATCH_TCINS:
    #     return await api_client.check_tcins(WATCH_TCINS)
    return await api_client.search_all_pokemon_tcg(count=40)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
@tasks.loop(seconds=1)
async def monitor_task():
    global _seeded

    # Quiet hours — sleep until 7 AM PST
    secs = _quiet_hours_remaining()
    if secs is not None:
        wake_str = datetime.now(PST).replace(
            hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0
        ).strftime("%I:%M %p %Z")
        logger.info("Quiet hours (2–7 AM PST) — sleeping %.0f min until %s", secs / 60, wake_str)
        await asyncio.sleep(secs)
        return

    # Normal poll with jitter
    sleep_for = max(30, POLL_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS))
    logger.info("Next poll in %.0fs", sleep_for)
    await asyncio.sleep(sleep_for)

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        logger.error("Channel %s not found — check DISCORD_CHANNEL_ID", CHANNEL_ID)
        return

    try:
        products = await _fetch_products()
    except CookiesExpiredError:
        logger.warning("Cookies expired — auto-refreshing via Playwright…")
        if not await _refresh_cookies():
            logger.error("Auto-refresh failed — skipping this poll")
            return
        try:
            products = await _fetch_products()
        except CookiesExpiredError:
            logger.error("Still 403 after refresh — skipping poll")
            return

    logger.info("Fetched %d products", len(products))

    if not _seeded:
        _seed_baseline(products)
        _seeded = True
    else:
        await _process_products(channel, products)

    await _save_state()


@tasks.loop(hours=COOKIE_REFRESH_HOURS)
async def cookie_refresh_task():
    global _cookie_refresh_count
    _cookie_refresh_count += 1
    if _cookie_refresh_count == 1:
        return  # Skip the first immediate fire — cookies just loaded on startup
    logger.info("Scheduled cookie refresh (every %dh)…", COOKIE_REFRESH_HOURS)
    await _refresh_cookies()


@monitor_task.before_loop
async def before_monitor():
    await bot.wait_until_ready()
    logger.info(
        "Bot ready — monitor starting (interval ~%ds ±%ds, quiet 2–7 AM PST)",
        POLL_SECONDS, JITTER_SECONDS,
    )


@cookie_refresh_task.before_loop
async def before_cookie_refresh():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.command(name="check")
@commands.has_permissions(manage_messages=True)
async def cmd_check(ctx: commands.Context):
    """Trigger an immediate stock check."""
    await ctx.send("Running manual check…")
    try:
        products = await _fetch_products()
    except CookiesExpiredError:
        await ctx.send("⚠️ Cookies expired — refreshing automatically…")
        if not await _refresh_cookies(notify_channel=ctx.channel):
            return
        try:
            products = await _fetch_products()
        except CookiesExpiredError:
            await ctx.send("❌ Still blocked after refresh — check logs.")
            return

    if not products:
        await ctx.send("No products returned from Target API.")
        return

    in_stock    = [p for p in products if p["ship_status"] in IN_STOCK_STATUSES]
    unavailable = [p for p in products if p["ship_status"] not in IN_STOCK_STATUSES]

    if in_stock:
        for p in in_stock:
            await ctx.send(embed=_restock_embed(p))
    else:
        await ctx.send(
            f"Nothing available online right now ({len(products)} products checked)."
        )

    if unavailable:
        lines = [
            f"`{p['tcin']}` {p['ship_status']} — {p['title'][:50]}"
            for p in unavailable[:10]
        ]
        await ctx.send("**Staged / unavailable:**\n" + "\n".join(lines))


@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    """Show current known stock state."""
    if not stock_state:
        await ctx.send("No stock data yet — waiting for first poll.")
        return
    in_stock = [t for t, s in stock_state.items() if s["ship"]]
    msg = f"**Watching {len(stock_state)} products** — {len(in_stock)} currently in stock."
    if in_stock:
        msg += "\n**In stock:** " + ", ".join(f"`{t}`" for t in in_stock[:20])
    if _quiet_hours_remaining() is not None:
        msg += "\n😴 **Quiet hours active** — polling resumes at 7:00 AM PST."
    await ctx.send(msg)


@bot.command(name="searchterms")
async def cmd_searchterms(ctx: commands.Context):
    """Show active keyword search terms."""
    terms = target_api.SEARCH_TERMS
    lines = "\n".join(f"• `{t}`" for t in terms)
    await ctx.send(
        f"**Active search terms ({len(terms)}):**\n{lines}\n\n"
        "To add/change terms, update `SEARCH_TERMS` in Railway Variables "
        "(comma-separated) and redeploy."
    )


@bot.command(name="watchlist")
async def cmd_watchlist(ctx: commands.Context):
    """Show monitoring mode and settings."""
    await ctx.send(
        "**Mode:** Keyword search — `pokemon trading card game` + `pokemon booster`\n"
        "Automatically discovers any new Pokemon TCG product in Target's system.\n"
        f"*(TCIN mode available but disabled — {len(WATCH_TCINS)} TCINs on standby)*\n"
        "**Quiet hours:** 2:00 AM – 7:00 AM PST (no polls, no alerts)"
    )


@bot.command(name="refreshcookies")
@commands.has_permissions(manage_messages=True)
async def cmd_refresh_cookies(ctx: commands.Context):
    """Manually trigger a Playwright cookie refresh."""
    await _refresh_cookies(notify_channel=ctx.channel)


@bot.command(name="forget")
@commands.has_permissions(manage_messages=True)
async def cmd_forget(ctx: commands.Context):
    """Wipe Redis state and re-seed on next poll."""
    global _seeded
    stock_state.clear()
    _seeded = False
    await _save_state()
    await ctx.send("State cleared — will re-seed silently on next poll.")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    global api_client, redis_client, stock_state, _seeded, _startup_done
    if _startup_done:
        logger.info("Reconnected as %s", bot.user)
        return
    _startup_done = True

    api_client   = TargetAPIClient()
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Restore stock state
    saved = await _load_state()
    if saved:
        stock_state.update(saved)
        _seeded = True
        logger.info("Restored %d products from Redis", len(saved))
    else:
        logger.info("No Redis state — will seed on first poll")

    # Load cookies: Redis → env var → Playwright (in that order)
    cached = await redis_client.get(REDIS_COOKIES_KEY)
    if cached:
        target_api.update_cookies(cached)
        logger.info("Cookies loaded from Redis")
    elif target_api.current_cookies():
        logger.info("Using TARGET_COOKIES from env var")
    else:
        logger.info("No cookies found — running Playwright on startup…")
        await _refresh_cookies()

    logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    monitor_task.start()
    cookie_refresh_task.start()


@bot.event
async def on_disconnect():
    if "api_client" in globals() and api_client:
        await api_client.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
