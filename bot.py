"""Discord key-generation bot.

Commands:
  ,spoof [count]       Generate one or more keys in the current channel.
  ,whitelist @role     Allow every member with a role to run ,spoof.
  ,unwhitelist @role   Remove a role's access (owner/admin only).
  ,whitelist list      List allowed users (owner/admin only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
API_SECRET = os.environ["LICENSE_API_SECRET"]
API_URL = os.getenv(
    "LICENSE_API_URL",
    "https://muispoof-license.yourllytried.workers.dev/admin/generate",
)
MAX_COUNT = int(os.getenv("MAX_KEY_COUNT", "100"))
DATA_FILE = Path(os.getenv("WHITELIST_FILE", "whitelist.json"))


def _ids_from_env(name: str) -> set[int]:
    return {int(value.strip()) for value in os.getenv(name, "").split(",") if value.strip()}


OWNER_IDS = _ids_from_env("OWNER_IDS")


def load_whitelist() -> tuple[set[int], set[int]]:
    if not DATA_FILE.exists():
        return set(), set()
    try:
        values = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        # Accept the old list format as user IDs for easy upgrades.
        if isinstance(values, list):
            return {int(value) for value in values}, set()
        return ({int(value) for value in values.get("users", [])},
                {int(value) for value in values.get("roles", [])})
    except (OSError, ValueError, TypeError):
        return set(), set()


def save_whitelist(users: set[int], roles: set[int]) -> None:
    DATA_FILE.write_text(json.dumps({"users": sorted(users), "roles": sorted(roles)}, indent=2), encoding="utf-8")


whitelisted_users, whitelisted_roles = load_whitelist()
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=",", intents=intents, help_command=None)


def is_owner(ctx: commands.Context) -> bool:
    return ctx.author.id in OWNER_IDS


def can_generate(ctx: commands.Context) -> bool:
    if ctx.author.id in whitelisted_users or is_owner(ctx):
        return True
    return isinstance(ctx.author, discord.Member) and any(
        role.id in whitelisted_roles for role in ctx.author.roles
    )


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.command(name="spoof")
async def spoof(ctx: commands.Context, count: int = 1) -> None:
    """Generate keys and send them to the channel where the command was used."""
    if not can_generate(ctx):
        await ctx.reply("You are not whitelisted to generate keys.", mention_author=False)
        return
    if count < 1 or count > MAX_COUNT:
        await ctx.reply(f"Count must be between 1 and {MAX_COUNT}.", mention_author=False)
        return

    async with ctx.typing():
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(API_URL, json={"secret": API_SECRET, "count": count}) as response:
                    body = await response.text()
                    if response.status >= 400:
                        await ctx.reply(f"Key service returned HTTP {response.status}.", mention_author=False)
                        return
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        payload = body
        except (aiohttp.ClientError, TimeoutError) as exc:
            print(f"License API request failed: {exc}")
            await ctx.reply("Could not reach the key service. Try again later.", mention_author=False)
            return

    keys = extract_keys(payload)
    if not keys:
        await ctx.reply("The key service returned no keys.", mention_author=False)
        return
    message = "\n".join(f"`{key}`" for key in keys)
    await ctx.send(f"Generated {len(keys)} key{'s' if len(keys) != 1 else ''}:\n{message}")


def extract_keys(payload: object) -> list[str]:
    """Handle common JSON response shapes from license APIs."""
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if isinstance(payload, str):
        return [line.strip() for line in payload.splitlines() if line.strip()]
    if isinstance(payload, dict):
        for field in ("keys", "licenses", "results", "data"):
            if field in payload:
                return extract_keys(payload[field])
        for field in ("key", "license"):
            if field in payload:
                return [str(payload[field])]
    return []


@bot.group(name="whitelist", invoke_without_command=True)
async def whitelist_command(ctx: commands.Context) -> None:
    """Manage users allowed to run ,spoof."""
    if not is_owner(ctx):
        await ctx.reply("Only the configured bot owner can manage the whitelist.", mention_author=False)
        return
    if not ctx.message.role_mentions and not ctx.message.mentions:
        await ctx.reply("Usage: `,whitelist @role` or `,whitelist @user`.", mention_author=False)
        return
    if ctx.message.role_mentions:
        role = ctx.message.role_mentions[0]
        whitelisted_roles.add(role.id)
        save_whitelist(whitelisted_users, whitelisted_roles)
        await ctx.reply(f"Members with {role.mention} can now use `,spoof`.", mention_author=False)
    else:
        user = ctx.message.mentions[0]
        whitelisted_users.add(user.id)
        save_whitelist(whitelisted_users, whitelisted_roles)
        await ctx.reply(f"{user.mention} can now use `,spoof`.", mention_author=False)


@whitelist_command.command(name="list")
async def whitelist_list(ctx: commands.Context) -> None:
    if not is_owner(ctx):
        await ctx.reply("Only the configured bot owner can manage the whitelist.", mention_author=False)
        return
    users = ", ".join(f"<@{user_id}>" for user_id in sorted(whitelisted_users)) or "none"
    roles = ", ".join(f"<@&{role_id}>" for role_id in sorted(whitelisted_roles)) or "none"
    await ctx.reply(f"Whitelisted users: {users}\nWhitelisted roles: {roles}", mention_author=False)


@bot.command(name="unwhitelist")
async def unwhitelist(ctx: commands.Context) -> None:
    if not is_owner(ctx):
        await ctx.reply("Only the configured bot owner can manage the whitelist.", mention_author=False)
        return
    if ctx.message.role_mentions:
        role = ctx.message.role_mentions[0]
        whitelisted_roles.discard(role.id)
        await ctx.reply(f"{role.mention} was removed from the whitelist.", mention_author=False)
    elif ctx.message.mentions:
        user = ctx.message.mentions[0]
        whitelisted_users.discard(user.id)
        await ctx.reply(f"{user.mention} was removed from the whitelist.", mention_author=False)
    else:
        await ctx.reply("Usage: `,unwhitelist @role` or `,unwhitelist @user`.", mention_author=False)
        return
    save_whitelist(whitelisted_users, whitelisted_roles)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("Usage: `,spoof [count]`.", mention_author=False)
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("Count must be a whole number, and users must be mentioned.", mention_author=False)
    elif not isinstance(error, commands.CommandNotFound):
        print(f"Command error: {error}")


bot.run(BOT_TOKEN)
