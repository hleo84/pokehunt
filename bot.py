"""
Pokemon TCG Target Restock Bot
Polls Target's API every ~3 minutes and pings Discord when items come in stock.
State persists in Redis so restarts don't cause duplicate alerts.
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timezone

import discord
import redis.asyncio as aioredis
from discord.ext import commands, tasks
from dotenv import load_dotenv

from target_api import TargetAPIClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
NOTIFY_ROLE_ID = os.getenv("DISCORD_ROLE_ID", "")
POLL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))
JITTER_SECONDS = int(os.getenv("POLL_JITTER_SECONDS", "45"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_KEY = "pokemon_tcg_stock_state"

# Comma-separated TCINs to monitor specifically (optional — blank = keyword search)
WATCH_TCINS: list[str] = [
    t.strip() for t in os.getenv("WATCH_TCINS", "").split(",") if t.strip()
]

IN_STOCK_STATUSES = {"IN_STOCK", "LIMITED_STOCK"}

# ---------------------------------------------------------------------------
# Bot + globals
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
api_client: TargetAPIClient
redis_client: aioredis.Redis

# TCIN → {"ship": bool, "ever_seen": bool}
stock_state: dict[str, dict] = {}
_seeded = False  # True after first poll baseline is established


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------
async def _load_state() -> dict:
    try:
        raw = await redis_client.get(REDIS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        logger.exception("Failed to load state from Redis")
    return {}


async def _save_state():
    try:
        await redis_client.set(REDIS_KEY, json.dumps(stock_state))
    except Exception:
        logger.exception("Failed to save state to Redis")


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------
def _role_mention() -> str:
    return f"<@&{NOTIFY_ROLE_ID}>" if NOTIFY_ROLE_ID else "@here"


def _spotted_embed(product: dict) -> discord.Embed:
    embed = discord.Embed(
        title="👀 New Pokemon TCG Product Spotted",
        description=f"**{product['title']}**\nJust appeared in Target's system — not purchasable yet.",
        color=0xFFD700,  # gold
        url=product["url"],
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Online Status", value=product["ship_status"], inline=True)
    embed.add_field(name="Price", value=product["price"], inline=True)
    embed.add_field(name="Link", value=f"[target.com]({product['url']})", inline=False)
    embed.set_footer(text=f"TCIN {product['tcin']} • Target Restock Monitor")
    return embed


def _restock_embed(product: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🎴 Pokemon TCG — Now Available Online!",
        description=f"**{product['title']}**",
        color=0xCC0000,  # Target red
        url=product["url"],
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🚚 Online Status", value=product["ship_status"], inline=True)
    embed.add_field(name="💰 Price", value=product["price"], inline=True)
    embed.add_field(name="🔗 Buy Now", value=f"[target.com]({product['url']})", inline=False)
    embed.set_footer(text=f"TCIN {product['tcin']} • Target Restock Monitor")
    return embed


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def _seed_baseline(products: list[dict]):
    """Silently record all current products so we don't alert on startup."""
    for p in products:
        stock_state[p["tcin"]] = {
            "ship": p["ship_status"] in IN_STOCK_STATUSES,
            "ever_seen": True,
        }
    logger.info("Seeded baseline: %d products recorded, no alerts sent", len(products))


async def _process_products(channel: discord.TextChannel, products: list[dict]):
    for p in products:
        tcin = p["tcin"]
        prev = stock_state.get(tcin, {"ship": False, "ever_seen": False})

        ship_now = p["ship_status"] in IN_STOCK_STATUSES

        # Stage 1: brand-new product appeared in Target's system
        if not prev["ever_seen"]:
            logger.info("SPOTTED: %s (TCIN %s) status=%s", p["title"], tcin, p["ship_status"])
            await channel.send(embed=_spotted_embed(p))

        # Stage 2: product transitioned to purchasable online
        if ship_now and not prev["ship"]:
            logger.info("IN STOCK ONLINE: %s (TCIN %s)", p["title"], tcin)
            await channel.send(content=_role_mention(), embed=_restock_embed(p))

        stock_state[tcin] = {"ship": ship_now, "ever_seen": True}


async def _fetch_products() -> list[dict]:
    if WATCH_TCINS:
        return await api_client.check_tcins(WATCH_TCINS)
    return await api_client.search_all_pokemon_tcg(count=24)


# ---------------------------------------------------------------------------
# Poll task
# ---------------------------------------------------------------------------
@tasks.loop(seconds=1)
async def monitor_task():
    global _seeded
    sleep_for = POLL_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
    sleep_for = max(30, sleep_for)
    logger.info("Next poll in %.0fs", sleep_for)
    await asyncio.sleep(sleep_for)

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        logger.error("Channel %s not found — check DISCORD_CHANNEL_ID", CHANNEL_ID)
        return

    try:
        products = await _fetch_products()
        logger.info("Fetched %d unique products", len(products))

        if not _seeded:
            _seed_baseline(products)
            _seeded = True
        else:
            await _process_products(channel, products)

        await _save_state()
    except Exception:
        logger.exception("Error during poll")


@monitor_task.before_loop
async def before_monitor():
    await bot.wait_until_ready()
    logger.info("Bot ready — starting monitor (interval ~%ds ±%ds)", POLL_SECONDS, JITTER_SECONDS)


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
        if not products:
            await ctx.send("No products returned from Target API.")
            return

        in_stock = [p for p in products if p["ship_status"] in IN_STOCK_STATUSES]
        unavailable = [p for p in products if p["ship_status"] not in IN_STOCK_STATUSES]

        if in_stock:
            for p in in_stock:
                await ctx.send(embed=_restock_embed(p))
        else:
            await ctx.send(f"Nothing available online right now out of {len(products)} products checked.")

        if unavailable:
            lines = [f"`{p['tcin']}` {p['ship_status']} — {p['title'][:50]}" for p in unavailable[:10]]
            await ctx.send("**Staged / unavailable:**\n" + "\n".join(lines))
    except Exception as exc:
        await ctx.send(f"Error: {exc}")
        logger.exception("Manual check failed")


@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    """Show current known stock state from Redis."""
    if not stock_state:
        await ctx.send("No stock data yet — waiting for first poll.")
        return
    in_stock = [tcin for tcin, s in stock_state.items() if s["ship"]]
    watching = len(stock_state)
    msg = f"**Watching {watching} products** — {len(in_stock)} currently in stock."
    if in_stock:
        msg += "\n**In stock:** " + ", ".join(f"`{t}`" for t in in_stock[:20])
    await ctx.send(msg)


@bot.command(name="watchlist")
async def cmd_watchlist(ctx: commands.Context):
    """Show monitoring mode."""
    if WATCH_TCINS:
        await ctx.send("**Watching TCINs:** " + ", ".join(f"`{t}`" for t in WATCH_TCINS))
    else:
        await ctx.send(
            "**Mode:** Dual keyword search — `pokemon trading card game` + `pokemon booster`\n"
            "Set `WATCH_TCINS` in env to monitor specific products instead."
        )


@bot.command(name="forget")
@commands.has_permissions(manage_messages=True)
async def cmd_forget(ctx: commands.Context):
    """Wipe Redis state and re-seed on next poll (use if state gets stale)."""
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
    global api_client, redis_client, stock_state, _seeded
    api_client = TargetAPIClient()
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    saved = await _load_state()
    if saved:
        stock_state.update(saved)
        _seeded = True
        logger.info("Restored %d products from Redis — skipping seed pass", len(saved))
    else:
        logger.info("No Redis state found — will seed on first poll")

    logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    logger.info("WATCH_TCINS=%s", WATCH_TCINS or "keyword search")
    monitor_task.start()


@bot.event
async def on_disconnect():
    if api_client:
        await api_client.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
