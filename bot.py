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
API_URL = os.environ["LICENSE_API_URL"]
COLOR_API_URL = os.environ["COLOR_API_URL"]
SPOOF_EXE_SOURCE_CHANNEL_ID = int(os.getenv("SPOOF_EXE_SOURCE_CHANNEL_ID", "0"))
COLOR_EXE_SOURCE_CHANNEL_ID = int(os.getenv("COLOR_EXE_SOURCE_CHANNEL_ID", "0"))
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
async def spoof(ctx: commands.Context, count_or_action: str = "1") -> None:
    """Generate keys and send them to the channel where the command was used."""
    if not can_generate(ctx):
        return
    if count_or_action.lower() == "exe":
        await forward_latest_exe(ctx, SPOOF_EXE_SOURCE_CHANNEL_ID)
        return
    try:
        count = int(count_or_action)
    except ValueError:
        await ctx.reply("Usage: `,spoof`, `,spoof 5`, or `,spoof exe`.", mention_author=False)
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
    await ctx.send("\n".join(keys))


@bot.command(name="color")
async def color(ctx: commands.Context, action: str | None = None) -> None:
    """Generate a color key using the second service."""
    if not can_generate(ctx):
        return

    if action and action.lower() == "exe":
        await forward_latest_exe(ctx, COLOR_EXE_SOURCE_CHANNEL_ID)
        return

    async with ctx.typing():
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(COLOR_API_URL, json={"secret": API_SECRET}) as response:
                    body = await response.text()
                    if response.status >= 400:
                        await ctx.reply(f"Color service returned HTTP {response.status}.", mention_author=False)
                        return
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        payload = body
        except (aiohttp.ClientError, TimeoutError) as exc:
            print(f"Color API request failed: {exc}")
            await ctx.reply("Could not reach the color service. Try again later.", mention_author=False)
            return

    keys = extract_keys(payload)
    if keys:
        await ctx.send("\n".join(keys))
    else:
        await ctx.reply("The color service returned no key.", mention_author=False)


async def forward_latest_exe(ctx: commands.Context, source_channel_id: int) -> None:
    if not source_channel_id:
        await ctx.reply("The source channel for this service is not configured.", mention_author=False)
        return
    source = bot.get_channel(source_channel_id)
    if not isinstance(source, discord.TextChannel):
        await ctx.reply("The configured EXE source channel could not be found.", mention_author=False)
        return

    async for message in source.history(limit=100):
        exe_attachments = [
            attachment for attachment in message.attachments
            if attachment.filename.lower().endswith(".exe")
        ]
        if not exe_attachments:
            continue
        try:
            files = [await attachment.to_file(spoiler=False) for attachment in exe_attachments]
            posted_at = int(message.created_at.timestamp())
            timestamp_line = f"Posted: <t:{posted_at}:F>"
            content = f"{timestamp_line}\n{message.content}" if message.content else timestamp_line
            await ctx.send(content=content, files=files)
        except discord.HTTPException:
            await ctx.reply("Discord could not upload that file. Check its size and bot permissions.", mention_author=False)
        return

    await ctx.reply("No .exe file was found in the configured source channel.", mention_author=False)


def extract_keys(payload: object) -> list[str]:
    """Handle common JSON response shapes from license APIs."""
    if isinstance(payload, list):
        keys: list[str] = []
        for item in payload:
            keys.extend(extract_keys(item))
        return keys
    if isinstance(payload, str):
        return [line.strip() for line in payload.splitlines() if line.strip()]
    if isinstance(payload, dict):
        if "key" in payload:
            return [str(payload["key"])]
        for field in ("keys", "licenses", "results", "data"):
            if field in payload:
                return extract_keys(payload[field])
        for field in ("license",):
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


@bot.command(name="hwid")
async def hwid(ctx: commands.Context, key: str, action: str) -> None:
    """Reset a license's device binding on either configured service."""
    if not is_owner(ctx):
        return
    if action.lower() != "reset":
        await ctx.reply("Usage: `,hwid <key> reset`.", mention_author=False)
        return

    reset_urls = set()
    for service_url in (API_URL, COLOR_API_URL):
        base_url = service_url.split("/admin/", 1)[0].rstrip("/")
        reset_urls.add(f"{base_url}/admin/reset")

    successes = 0
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for reset_url in reset_urls:
                async with session.post(
                    reset_url,
                    json={"secret": API_SECRET, "key": key, "license": key},
                ) as response:
                    if response.status < 400:
                        try:
                            payload = await response.json()
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            payload = {}
                        if payload.get("ok") is True:
                            successes += 1
    except (aiohttp.ClientError, TimeoutError) as exc:
        print(f"HWID reset request failed: {exc}")
        await ctx.reply("Could not reach the license services.", mention_author=False)
        return

    if successes:
        await ctx.reply("HWID reset.", mention_author=False)
    else:
        await ctx.reply("That key was not found on the configured services.", mention_author=False)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("Usage: `,spoof [count]`.", mention_author=False)
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("Count must be a whole number, and users must be mentioned.", mention_author=False)
    elif not isinstance(error, commands.CommandNotFound):
        print(f"Command error: {error}")


bot.run(BOT_TOKEN)
