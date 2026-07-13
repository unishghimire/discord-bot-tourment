# NexPlay Tournament Bot

Multi-server Discord tournament management bot for the NexPlay SaaS platform.

## Deployment (Render)
- Service type: **Background Worker**
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`

## Environment Variables (required)
```
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
BASE44_SERVICE_TOKEN=
```

## Features
- 10 slash commands (global — works in any server)
- Auto-registers new servers on join (trial: 3 tournaments)
- Subscription gate on all staff commands
- AI support agent in #support-ticket
- Dynamic channel resolution (no hardcoded IDs)
- Pollinations.ai image generation
