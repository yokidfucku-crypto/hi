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
import re
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


async def admin_requests(path: str, payload: dict) -> list[dict]:
    bases = {service_url.split("/admin/", 1)[0].rstrip("/") for service_url in (API_URL, COLOR_API_URL)}
    results = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for base_url in bases:
            try:
                async with session.post(f"{base_url}/admin/{path}", json={"secret": API_SECRET, **payload}) as response:
                    body = await response.json(content_type=None)
                    results.append({"status": response.status, "body": body, "base": base_url})
            except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
                continue
    return results


@bot.command(name="hwid")
async def hwid(ctx: commands.Context, first: str, second: str | None = None) -> None:
    """Display/reset a key HWID, or ban/unban an HWID."""
    if not is_owner(ctx):
        return
    if first.lower() in {"ban", "unban"}:
        if not second:
            await ctx.reply("Usage: `,hwid ban <hwid>` or `,hwid unban <hwid>`.", mention_author=False)
            return
        path = "ban-hwid" if first.lower() == "ban" else "unban-hwid"
        results = await admin_requests(path, {"hwid": second})
        if any(result["body"].get("ok") is True for result in results):
            await ctx.reply("HWID banned and linked keys deleted." if path == "ban-hwid" else "HWID unbanned.", mention_author=False)
        else:
            await ctx.reply("The HWID request failed.", mention_author=False)
        return

    key = first
    if second and second.lower() != "reset":
        await ctx.reply("Usage: `,hwid <key>`, `,hwid <key> reset`, `,hwid ban <hwid>`.", mention_author=False)
        return
    if second:
        results = await admin_requests("reset", {"key": key, "license": key})
        await ctx.reply("HWID reset." if any(result["body"].get("ok") is True for result in results) else "That key was not found.", mention_author=False)
        return

    results = await admin_requests("list", {})
    matches = [record for result in results for record in result["body"].get("licenses", [])
               if str(record.get("key") or record.get("license") or "").upper() == key.upper()]
    if not matches:
        await ctx.reply("That key was not found.", mention_author=False)
        return
    bound = next((record.get("hwid") for record in matches if record.get("hwid")), None)
    try:
        await ctx.author.send(f"HWID for `{key}`: `{bound}`" if bound else f"No HWID is bound to `{key}`.")
        await ctx.reply("I sent the HWID to your DMs.", mention_author=False)
    except discord.Forbidden:
        await ctx.reply("I could not DM you the HWID.", mention_author=False)


@bot.group(name="key", aliases=["keys"], invoke_without_command=True)
async def key_command(ctx: commands.Context, action: str | None = None, key: str | None = None) -> None:
    if not is_owner(ctx):
        return
    if action is None:
        results = await admin_requests("list", {})
        records = [record for result in results for record in result["body"].get("licenses", [])]
        if not records:
            await ctx.reply("No keys found.", mention_author=False)
            return
        lines = []
        seen = set()
        for record in records:
            value = str(record.get("key") or record.get("license") or "")
            if not value or value in seen:
                continue
            seen.add(value)
            status = "VALID" if record.get("valid", not record.get("paused", False)) else "INVALID"
            lines.append(f"{status} {value}")
        text = "\n".join(lines)
        for start in range(0, len(text), 1900):
            await ctx.send(text[start:start + 1900])
        return

    if action.lower() not in {"delete", "pause", "unpause"} or not key:
        await ctx.reply("Usage: `,key`, `,key delete <key>`, `,key pause <key>`, `,key unpause <key>`.", mention_author=False)
        return
    results = await admin_requests(action.lower(), {"key": key, "license": key})
    await ctx.reply(f"Key {action.lower()}d." if any(result["body"].get("ok") is True for result in results) else "That key was not found.", mention_author=False)


@key_command.command(name="deletefile")
async def deletefile(ctx: commands.Context) -> None:
    if not is_owner(ctx):
        return
    if not ctx.message.attachments:
        await ctx.reply("Attach a .txt file containing the keys, then run `,key deletefile`.", mention_author=False)
        return
    attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith((".txt", ".log", ".csv")):
        await ctx.reply("Attach a .txt, .log, or .csv file containing the keys.", mention_author=False)
        return
    try:
        raw = await attachment.read()
        text = raw.decode("utf-8", errors="ignore")
    except (discord.HTTPException, UnicodeError):
        await ctx.reply("I could not read that attachment.", mention_author=False)
        return

    patterns = (
        r"(?:KURO|EGO)-[0-9A-Z]{3}-[0-9A-Z]{5}-[0-9A-Z]",
        r"(?:KURO|MSP)-[0-9A-F]{32}-[0-9A-F]{16}",
    )
    keys = list(dict.fromkeys(match for pattern in patterns for match in re.findall(pattern, text, re.IGNORECASE)))
    if not keys:
        await ctx.reply("No supported license keys were found.", mention_author=False)
        return
    if len(keys) > 500:
        await ctx.reply("The file contains more than 500 keys. Split it into smaller files.", mention_author=False)
        return

    deleted = 0
    for license_key in keys:
        results = await admin_requests("delete", {"key": license_key, "license": license_key})
        if any(result["body"].get("ok") is True for result in results):
            deleted += 1
    await ctx.reply(f"Deleted {deleted} of {len(keys)} keys.", mention_author=False)


@bot.command(name="cmds")
async def cmds(ctx: commands.Context) -> None:
    await ctx.send(",spoof\n,color\n,whitelist\n,unwhitelist\n,hwid\n,key\n,key deletefile\n,how color\n,faq color\n,cmds")


@bot.command(name="how")
async def how(ctx: commands.Context, product: str | None = None) -> None:
    if product is None or product.lower() != "color":
        await ctx.reply("Usage: `,how color`.", mention_author=False)
        return
    await ctx.send(
        "**How to run KuroShift**\n\n"
        "1. Download **kuroshift.exe**.\n"
        "2. Double-click it to launch.\n"
        "3. Click **Yes** when Windows asks for administrator permission.\n"
        "4. Paste your license key and click **enter** once.\n"
        "5. Wait for verification, then customize your settings.\n\n"
        "No installation or extraction needed. Your activation saves automatically."
    )


@bot.command(name="faq")
async def faq(ctx: commands.Context, product: str | None = None) -> None:
    if product is None or product.lower() != "color":
        await ctx.reply("Usage: `,faq color`.", mention_author=False)
        return
    await ctx.send(
        "**KuroShift FAQ**\n\n"
        "**How do I run it?**\n"
        "Open **kuroshift.exe**, allow administrator access, and enter your license key.\n\n"
        "**Do I need to enter my key every time?**\n"
        "No, your activation saves automatically.\n\n"
        "**It says “Checking…”—what do I do?**\n"
        "Wait for it to finish. Only click once.\n\n"
        "**My key isn’t working.**\n"
        "Check the key and your internet connection, then try again.\n\n"
        "**My key is bound to another machine.**\n"
        "Contact support for an HWID reset.\n\n"
        "**Will resetting settings remove my activation?**\n"
        "No, resetting settings or switching profiles keeps you activated."
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("Usage: `,spoof [count]`.", mention_author=False)
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("Count must be a whole number, and users must be mentioned.", mention_author=False)
    elif not isinstance(error, commands.CommandNotFound):
        print(f"Command error: {error}")


bot.run(BOT_TOKEN)
