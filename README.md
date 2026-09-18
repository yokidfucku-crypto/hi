# Discord key bot

This bot provides a prefix command that calls your license endpoint:

- `,spoof` generates 1 key.
- `,spoof 5` generates 5 keys.
- `,whitelist @role` allows everyone with a role to run `,spoof`.
- `,whitelist @user` allows one user to run `,spoof`.
- `,unwhitelist @role` or `,unwhitelist @user` removes access.
- `,whitelist list` shows the whitelist.

Server administrators can manage the whitelist. The Discord IDs in `OWNER_IDS` can manage it everywhere the bot is installed.

## Setup

1. Install Python 3.10 or newer.
2. Copy `.env.example` to `.env` and fill in `DISCORD_BOT_TOKEN`, `LICENSE_API_SECRET`, and your Discord user ID in `OWNER_IDS`.
3. In the Discord Developer Portal, enable **Message Content Intent** under Bot settings. The bot needs this because it uses prefix commands.
4. Invite the bot with the `bot` scope and permissions to view/send messages.
5. Install dependencies and run:

```powershell
py -m pip install -r requirements.txt
py bot.py
```

The first owner can run `,whitelist @user`. Whitelist data is stored in `whitelist.json` next to the bot.
