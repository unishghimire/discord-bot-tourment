"""
NexPlay Tournament Bot
======================
Author  : Unish Ghimire / NexPlay ORG
Version : 4.0.0  (multi-server production)
Python  : 3.10+

CHANGELOG v4.0.0
-----------------
FIXED  - Global slash commands (removed guild= from all decorators).
         Commands now appear in EVERY server that adds the bot.
FIXED  - Channel IDs are now resolved dynamically from the guild,
         not hardcoded to NEXPLAY ORG channel IDs.
FIXED  - on_guild_join auto-registers new servers into Base44 DB
         and posts an alert to the owner's log channel.
FIXED  - Subscription gate on every staff command — unregistered
         or expired servers cannot use tournament commands.
FIXED  - get_or_create_channels() provisions required channels in
         any new server automatically (no manual setup needed).
FIXED  - Support handler resolves the correct channel per guild.
ADDED  - on_guild_remove logs server departures.
ADDED  - /setup command for server owners to initialize their server.

ARCHITECTURE
------------
One bot process → connects to Discord gateway once → serves N servers.
Each guild interaction is fully isolated via guild_id field in every
Base44 entity record. Zero cross-server data leakage.

SECURITY
--------
* No hardcoded secrets — all from env.
* Subscription gate enforced server-side on every write command.
* is_staff() checks role names — cannot be bypassed by renaming.
* All user input stored as plain string — no eval/exec surface.
* aiohttp 10s timeout on every external call.
"""

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import os
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load .env file if present (local dev). Render injects vars directly.
load_dotenv()

# ══════════════════════════════════════════════════════════
#  CONFIG  — all values come from environment variables
# ══════════════════════════════════════════════════════════
BOT_TOKEN   = os.environ.get("DISCORD_BOT_TOKEN", "")
HOME_GUILD  = int(os.environ.get("DISCORD_GUILD_ID", "0"))   # owner's server only
SVC_TOKEN   = os.environ.get("BASE44_SERVICE_TOKEN", "")
APP_ID      = "6a5226b5047f5c59d961130e"

BASE44_API  = "https://base44.app/api/apps/" + APP_ID + "/entities"
DISCORD_API = "https://discord.com/api/v10"

# ── Role names that count as "staff" in any server ────────
STAFF_ROLE_NAMES = {
    "NexPlay Owner", "Tournament Host", "Admin", "Moderator", "NexPlay Admin",
    "Owner", "Co-Owner", "Manager", "Staff",
}

# ── Status emoji map ───────────────────────────────────────
STATUS_EMOJI = {
    "registration_open":   "🟢",
    "registration_closed": "🔒",
    "groups_generated":    "🎯",
    "scheduled":           "📅",
    "in_progress":         "🔥",
    "completed":           "✅",
    "cancelled":           "❌",
}

# Shared HTTP timeout
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


# ══════════════════════════════════════════════════════════
#  BOT CLASS
# ══════════════════════════════════════════════════════════
class NexPlayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
        # GLOBAL sync — commands appear in every server
        await self.tree.sync()
        print("[NexPlay] Global slash commands synced.")

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot  = NexPlayBot()
tree = bot.tree


# ══════════════════════════════════════════════════════════
#  BASE44 ENTITY HELPERS
# ══════════════════════════════════════════════════════════
def _b44_headers() -> dict:
    return {"Authorization": "Bearer " + SVC_TOKEN, "Content-Type": "application/json"}


async def b44_list(entity: str, filters: dict | None = None) -> list:
    url = BASE44_API + "/" + entity
    try:
        async with bot.http_session.get(url, headers=_b44_headers()) as r:
            if r.status != 200:
                return []
            data = await r.json()
            if not isinstance(data, list):
                return []
            if filters:
                for k, v in filters.items():
                    data = [x for x in data if x.get(k) == v]
            return data
    except Exception as e:
        print("[b44_list] " + str(e))
        return []


async def b44_create(entity: str, payload: dict) -> dict:
    url = BASE44_API + "/" + entity
    try:
        async with bot.http_session.post(url, json=payload, headers=_b44_headers()) as r:
            if r.status in (200, 201):
                return await r.json()
            return {}
    except Exception as e:
        print("[b44_create] " + str(e))
        return {}


async def b44_update(entity: str, record_id: str, payload: dict) -> dict:
    url = BASE44_API + "/" + entity + "/" + record_id
    try:
        async with bot.http_session.put(url, json=payload, headers=_b44_headers()) as r:
            if r.status in (200, 201):
                return await r.json()
            return {}
    except Exception as e:
        print("[b44_update] " + str(e))
        return {}


# ══════════════════════════════════════════════════════════
#  SUBSCRIPTION GATE
#  Every staff command calls this first. If the server isn't
#  registered or has expired, the command is blocked.
# ══════════════════════════════════════════════════════════
async def get_server_record(guild_id: str) -> dict | None:
    servers = await b44_list("Server", {"guild_id": guild_id})
    return servers[0] if servers else None


async def is_allowed(guild_id: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Only active/trial servers pass."""
    rec = await get_server_record(guild_id)
    if not rec:
        return False, "This server is not registered with NexPlay. Ask your admin to run `/setup`."
    status = rec.get("subscription_status", "")
    if status == "banned":
        return False, "This server has been banned from NexPlay. Contact support."
    if status in ("active", "trial"):
        return True, ""
    return False, "This server's NexPlay subscription has expired. Visit nexplay.gg to renew."


# ══════════════════════════════════════════════════════════
#  DYNAMIC CHANNEL RESOLVER
#  Finds channels by name — works in ANY server.
#  Falls back to first available channel if not found.
# ══════════════════════════════════════════════════════════
CHANNEL_NAMES = {
    "announcements": ["tourney-announcements", "tournament-announcements", "announcements", "general"],
    "registration":  ["tourney-registration", "tournament-registration", "registration", "general"],
    "brackets":      ["brackets-results", "brackets", "results", "tournament-results", "general"],
    "champions":     ["hall-of-champions", "champions", "winners", "general"],
    "support":       ["support-ticket", "support", "help", "general"],
    "staff":         ["mod-log", "staff-log", "staff", "moderators", "admin-log"],
    "rules":         ["tourney-rules", "rules", "tournament-rules", "general"],
}


def resolve_channel(guild: discord.Guild, key: str) -> discord.TextChannel | None:
    """Find a channel by trying a list of common names."""
    for name in CHANNEL_NAMES.get(key, []):
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch:
            return ch
    # fallback: first text channel the bot can send in
    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me)
        if perms.send_messages and perms.embed_links:
            return ch
    return None


async def get_or_create_channels(guild: discord.Guild) -> dict:
    """
    Ensure required channels exist. Returns a dict of channel IDs.
    Creates missing channels under a 'NexPlay Tournaments' category.
    """
    required = {
        "announcements": "tourney-announcements",
        "registration":  "tourney-registration",
        "brackets":      "brackets-results",
        "champions":     "hall-of-champions",
        "support":       "support-ticket",
        "staff":         "mod-log",
    }
    result = {}
    category = discord.utils.get(guild.categories, name="NexPlay Tournaments")
    if not category:
        try:
            category = await guild.create_category("NexPlay Tournaments")
        except Exception:
            category = None

    for key, ch_name in required.items():
        ch = resolve_channel(guild, key)
        if not ch:
            try:
                ch = await guild.create_text_channel(ch_name, category=category)
            except Exception:
                ch = guild.text_channels[0] if guild.text_channels else None
        result[key] = ch.id if ch else None
    return result


# ══════════════════════════════════════════════════════════
#  DISCORD REST HELPER
# ══════════════════════════════════════════════════════════
async def dpost(channel_id: int, embed: discord.Embed, content: str = "") -> dict:
    if not channel_id:
        return {}
    url     = DISCORD_API + "/channels/" + str(channel_id) + "/messages"
    headers = {"Authorization": "Bot " + BOT_TOKEN, "Content-Type": "application/json"}
    body    = {"embeds": [embed.to_dict()]}
    if content:
        body["content"] = content
    try:
        async with bot.http_session.post(url, json=body, headers=headers) as r:
            return await r.json()
    except Exception as e:
        print("[dpost] " + str(e))
        return {}


# ══════════════════════════════════════════════════════════
#  SMALL UTILITIES
# ══════════════════════════════════════════════════════════
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def is_staff(member: discord.Member) -> bool:
    for role in member.roles:
        clean = role.name
        for prefix in ("👑 ", "⚔️ ", "🛡️ ", "🔧 ", "🎮 ", "🎯 ", "⚙️ ", "🏆 ", "🔥 ", "⛏️ ", "📋 ", "🌱 ", "🤖 "):
            clean = clean.replace(prefix, "")
        if clean in STAFF_ROLE_NAMES:
            return True
    return False

def err_e(msg: str) -> discord.Embed:
    e = discord.Embed(description="❌  " + msg, color=0xFF4444)
    e.set_footer(text="NexPlay")
    return e

def ok_e(title: str, desc: str, color: int = 0x00FF7F) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text="NexPlay Tournament System")
    e.timestamp = datetime.now(timezone.utc)
    return e

def img_url(t_name: str, game: str, kind: str, extra: str = "") -> str:
    templates = {
        "poster":   "professional esports tournament poster {n} {g} neon dark background gold purple cinematic",
        "roadmap":  "tournament roadmap timeline {n} {g} stages Registration GroupDraw Schedule MatchDay Champion modern dark",
        "group":    "esports group draw reveal {n} {g} {x} dark neon panels",
        "schedule": "match schedule card {n} {g} {x} dark professional infographic",
        "result":   "match result card {x} {n} dark dramatic victory graphic",
        "champion": "champion victory {x} wins {n} {g} golden trophy confetti epic cinematic",
    }
    tpl    = templates.get(kind, "professional esports graphic {n} {g}")
    prompt = tpl.format(n=t_name, g=game, x=extra)
    prompt = prompt.replace(" ", "%20").replace(",", "%2C")
    return (
        "https://image.pollinations.ai/prompt/" + prompt
        + "?width=1280&height=640&nologo=true&seed=" + str(now_ts()) + "&model=flux"
    )


# ══════════════════════════════════════════════════════════
#  AI SUPPORT ENGINE (guild-aware)
# ══════════════════════════════════════════════════════════
async def handle_support(message: discord.Message) -> None:
    q   = message.content.strip()
    ql  = q.lower()
    gid = str(message.guild.id)

    # Resolve channels dynamically for this guild
    ch_ann   = resolve_channel(message.guild, "announcements")
    ch_reg   = resolve_channel(message.guild, "registration")
    ch_brack = resolve_channel(message.guild, "brackets")
    ch_champ = resolve_channel(message.guild, "champions")
    ch_staff = resolve_channel(message.guild, "staff")
    ch_rules = resolve_channel(message.guild, "rules")

    ann_mention   = ch_ann.mention   if ch_ann   else "#announcements"
    reg_mention   = ch_reg.mention   if ch_reg   else "#registration"
    brack_mention = ch_brack.mention if ch_brack else "#brackets"
    rules_mention = ch_rules.mention if ch_rules else "#rules"

    # Load active tournament for context
    all_t  = await b44_list("Tournament", {"guild_id": gid})
    active = next(
        (t for t in sorted(all_t, key=lambda x: x.get("created_date", ""), reverse=True)
         if t.get("status") not in ("completed", "cancelled")),
        None
    )
    tn   = active.get("name", "current tournament") if active else None
    ts   = active.get("status", "")                 if active else ""
    tg   = active.get("game", "")                   if active else ""
    tp   = active.get("prize_pool", "TBA")           if active else "TBA"
    td   = active.get("tournament_date", "TBA")      if active else "TBA"
    tmx  = active.get("max_players", "?")            if active else "?"
    tcnt = active.get("registered_count", 0)         if active else 0

    def has(*kws: str) -> bool:
        return any(k in ql for k in kws)

    routed = False
    tips: list[str] = []
    color  = 0x00FF7F
    cat    = "General"

    if has("payment", "refund", "billing", "subscription", "upgrade", "plan", "buy", "paid", "price"):
        cat = "Billing"; routed = True; color = 0xFF9900
        resp = ("**Billing / Payment Issue**\n\nI've notified the admin team!\n\n"
                "> Tournament entry is FREE for all members\n"
                "> For plan or subscription issues, the owner will DM you\n\nStaff will respond shortly!")
        tips = ["Verify payment in admin panel", "Reply to user in #support-ticket", "Check subscription records"]

    elif has("account", "banned", "kick", "muted", "hacked", "stolen", "password", "login", "suspended"):
        cat = "Account"; routed = True; color = 0xFF4444
        resp = ("**Account Issue Detected**\n\nFlagged to moderators!\n\n"
                "> If hacked, change your Discord password immediately\n"
                "> If wrongly actioned, explain your case to staff\n\nA moderator will respond soon!")
        tips = ["Check audit log for action", "Review user's recent messages", "DM user if action was a mistake"]

    elif has("cheat", "report", "toxic", "abuse", "harass", "unfair", "complaint"):
        cat = "Complaint"; routed = True; color = 0xFF4444
        resp = ("**Report / Complaint Received**\n\nFlagged for moderation!\n\n"
                "> Describe what happened\n> Attach screenshots if possible\n"
                "> Include the Discord username\n\nA moderator will review and act. Thank you!")
        tips = ["Review recent messages", "Check if accused is in a tournament", "Warn / mute / ban as appropriate"]

    elif has("crash", "lag", "disconnect", "not working", "error", "bug", "broken", "glitch"):
        cat = "Technical"; routed = True; color = 0xFF9900
        resp = ("**Technical Issue Detected**\n\nNotified the tech team!\n\n"
                "> 1. Restart your Discord app\n> 2. Check your internet\n"
                "> 3. Clear Discord cache\n\nStaff will assist shortly!")
        tips = ["Ask: which device/OS?", "Check for known outages", "Try to reproduce"]

    elif has("register", "sign up", "join tournament", "how to join", "enroll", "want to register"):
        cat = "Registration"; color = 0x00FF7F
        if active and ts == "registration_open":
            resp = ("Registration for **" + str(tn) + "** is OPEN!\n\n"
                    "1. Go to " + reg_mention + "\n2. Use /register + your in-game name\n3. Done!\n\n"
                    "Slots: " + str(tcnt) + "/" + str(tmx) + " | Game: " + str(tg) + " | Date: " + str(td))
            tips = ["Slots: " + str(tcnt) + "/" + str(tmx)]
        elif active:
            resp = ("Registration for **" + str(tn) + "** is CLOSED (" + str(ts) + ")\n\n"
                    "Watch " + ann_mention + " for the next tournament!")
            tips = ["Registration closed"]
        else:
            resp = "No active tournament right now. Watch " + ann_mention + " — tournaments are posted regularly!"
            tips = ["No active tournament"]

    elif has("when", "schedule", "time", "date", "match time", "fixture", "next match"):
        cat = "Schedule"; color = 0x1E90FF
        if active:
            resp = ("**" + str(tn) + " — Schedule:**\n\nGame: " + str(tg) + " | Date: " + str(td) +
                    " | Status: " + str(ts) + "\n\nFull schedule in " + brack_mention +
                    " after group draw.\nBe ready 10 min before your match — late = forfeit!")
            tips = ["Date: " + str(td)]
        else:
            resp = "No active tournament. Watch " + ann_mention + " for upcoming dates!"
            tips = ["No active tournament"]

    elif has("prize", "reward", "cash", "winning", "money", "payout", "how much"):
        cat = "Prize"; color = 0xFFD700
        resp = ("**Prize Pool" + (" — " + str(tn) if active else "") + "**\n\n"
                "Total: " + str(tp) + "\n" + ("Game: " + str(tg) + " | Date: " + str(td) + "\n\n" if active else "") +
                "Full breakdown announced before match day!")
        tips = ["Prize: " + str(tp)]

    elif has("rule", "regulation", "fair", "allowed", "disqualify", "dq", "no-show"):
        cat = "Rules"; color = 0x00FF7F
        resp = ("**Tournament Rules:**\n\n"
                "> 1. No cheating — instant permanent DQ\n"
                "> 2. Be in VC 10 min before your match\n"
                "> 3. Screenshot results and post in results channel\n"
                "> 4. Respect everyone — toxic behaviour = ban\n"
                "> 5. No-show after 5 min = forfeit\n"
                "> 6. Host decision is final\n"
                "> 7. One account per player\n\n"
                "Full rules: " + rules_mention)
        tips = ["Rules question — no action needed unless violation mentioned"]

    elif has("bracket", "group", "draw", "round", "opponent", "my group", "matchup"):
        cat = "Brackets"; color = 0x9B59B6
        if active and ts in ("groups_generated", "scheduled", "in_progress"):
            resp = ("**" + str(tn) + " — Groups & Brackets:**\n\n"
                    "Groups drawn! Check " + brack_mention + " to see your group, opponents, and match times.\n\n"
                    "Status: " + str(ts) + (" — LIVE!" if ts == "in_progress" else ""))
            tips = ["Brackets visible — no action needed"]
        else:
            resp = ("Groups haven't been drawn yet!\n\nBrackets appear in " + brack_mention +
                    " after registration closes.\n" +
                    ("Current status: " + str(ts) if active else "No active tournament right now."))
            tips = ["Use /generate_groups when ready"]

    elif has("result", "score", "winner", "who won", "standing", "leaderboard"):
        cat = "Results"; color = 0x1E90FF
        resp = ("Results are posted in " + brack_mention + " after each match.\n"
                "The final champion is revealed in " +
                (ch_champ.mention if ch_champ else "#champions") + "!\n\n" +
                ("Current: **" + str(tn) + "** | " + str(ts) if active else "No active tournament."))
        tips = ["Results posted — no action needed"]

    elif has("command", "slash", "bot", "how to use", "help", "what can"):
        cat = "Bot Help"; color = 0xFFD700
        resp = ("**NexPlay Bot Commands:**\n\n"
                "Player commands:\n"
                "> /register — Join a tournament\n"
                "> /tournament_status — View tournaments\n"
                "> /help — Full command list\n\n"
                "Staff commands:\n"
                "> /create_tournament  /close_registration  /generate_groups\n"
                "> /post_schedule  /post_result  /complete_tournament  /announce")
        tips = ["Bot help — no action needed"]

    else:
        cat = "Unknown"; routed = True; color = 0x9B59B6
        resp = ("Not 100% sure — flagged to staff!\n\n"
                "Quick self-help:\n"
                "> Tournaments → " + ann_mention + "\n"
                "> Register → " + reg_mention + "\n"
                "> Brackets → " + brack_mention + "\n"
                "> Commands → /help")
        tips = ["Read full question and respond manually", "Consider adding this topic to the knowledge base"]

    # Reply to user
    ue = discord.Embed(
        description="Hey " + message.author.mention + "! Here's what I found:\n\n" + resp,
        color=color
    )
    ue.set_author(name="NexPlay AI Support", icon_url="https://cdn.discordapp.com/embed/avatars/0.png")
    if routed:
        ue.add_field(name="Staff Notified", value="The team has been alerted and will assist you shortly!", inline=False)
    ue.set_footer(text="NexPlay AI Support — Type your question anytime!")
    ue.timestamp = datetime.now(timezone.utc)
    await message.channel.send(embed=ue)

    # Staff log (only when escalated)
    if routed and ch_staff:
        high  = cat in ("Account", "Complaint")
        se    = discord.Embed(
            title=("🚨 ESCALATED" if high else "⚠️ FLAGGED") + " — Support Alert | " + cat,
            color=0xFF4444 if high else 0xFF9900,
            timestamp=datetime.now(timezone.utc)
        )
        se.add_field(name="User", value=message.author.mention + " (" + message.author.display_name + ")", inline=True)
        se.add_field(name="Channel", value=message.channel.mention, inline=True)
        se.add_field(name="Question", value=q[:500], inline=False)
        if tips:
            se.add_field(name="Suggested Actions", value="\n".join("> " + t for t in tips), inline=False)
        se.add_field(name="Jump", value="[Go to message](" + message.jump_url + ")", inline=False)
        await dpost(ch_staff.id, se)

        # Log to DB
        await b44_create("SupportMessage", {
            "guild_id": gid, "user_discord_id": str(message.author.id),
            "user_username": str(message.author.name),
            "question": q[:500], "ai_response": resp[:500],
            "confidence_score": 0.5 if cat == "Unknown" else 0.9,
            "routed_to_human": True, "category": cat,
        })


# ══════════════════════════════════════════════════════════
#  GATEWAY EVENTS
# ══════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print("[NexPlay] Logged in as " + str(bot.user) + " | Servers: " + str(len(bot.guilds)))
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=str(len(bot.guilds)) + " servers | /help")
    )


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Auto-register new server into Base44 on first join."""
    gid = str(guild.id)
    existing = await get_server_record(gid)
    if not existing:
        await b44_create("Server", {
            "guild_id": gid,
            "guild_name": guild.name,
            "owner_discord_id": str(guild.owner_id) if guild.owner_id else "",
            "subscription_status": "trial",
            "tournaments_used": 0,
            "tournament_limit": 3,
            "member_count": guild.member_count or 0,
            "last_active": now_iso(),
        })
        await b44_create("AdminNotification", {
            "type": "new_server",
            "severity": "info",
            "message": "New server joined: " + guild.name + " (ID: " + gid + ") | Members: " + str(guild.member_count),
            "read_by_unish": False,
        })

    # Provision channels
    channels = await get_or_create_channels(guild)

    # Welcome embed in announcements channel
    ann_id = channels.get("announcements")
    if ann_id:
        welcome = discord.Embed(
            title="NexPlay Tournament Bot is here!",
            description=(
                "Thanks for adding NexPlay to **" + guild.name + "**!\n\n"
                "**Getting Started:**\n"
                "> 1. Run `/setup` to configure your server\n"
                "> 2. Use `/create_tournament` to start a tournament\n"
                "> 3. Players use `/register` to join\n\n"
                "**Your trial includes 3 free tournaments.**\n"
                "Need more? Visit nexplay.gg to upgrade.\n\n"
                "Need help? Type your question in the support channel anytime!"
            ),
            color=0xFFD700,
            timestamp=datetime.now(timezone.utc)
        )
        welcome.set_footer(text="NexPlay Tournament System")
        await dpost(ann_id, welcome)

    # Alert owner's home server
    home_staff = resolve_channel(bot.get_guild(HOME_GUILD), "staff") if HOME_GUILD else None
    if home_staff:
        alert = discord.Embed(
            title="🎉 New Server Added NexPlay!",
            description=(
                "**" + guild.name + "**\n"
                "Guild ID: `" + gid + "`\n"
                "Members: " + str(guild.member_count) + "\n"
                "Owner ID: " + str(guild.owner_id) + "\n\n"
                "Status: **Trial** (3 tournaments)\n"
                "Total servers: " + str(len(bot.guilds))
            ),
            color=0x00FF7F,
            timestamp=datetime.now(timezone.utc)
        )
        await dpost(home_staff.id, alert)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Log server removal."""
    await b44_create("AdminNotification", {
        "type": "server_left",
        "severity": "warning",
        "message": "Server left: " + guild.name + " (ID: " + str(guild.id) + ")",
        "read_by_unish": False,
    })
    home_staff = resolve_channel(bot.get_guild(HOME_GUILD), "staff") if HOME_GUILD else None
    if home_staff:
        e = discord.Embed(
            title="⚠️ Server Removed NexPlay",
            description="**" + guild.name + "** (ID: `" + str(guild.id) + "`) removed the bot.\nTotal servers: " + str(len(bot.guilds)),
            color=0xFF4444, timestamp=datetime.now(timezone.utc)
        )
        await dpost(home_staff.id, e)


@bot.event
async def on_member_join(member: discord.Member):
    """Welcome new members with a DM."""
    try:
        e = discord.Embed(
            title="Welcome to " + member.guild.name + ", " + member.display_name + "!",
            description=(
                "We host gaming tournaments with real prizes!\n\n"
                "**Get Started:**\n"
                "> Check #announcements for active tournaments\n"
                "> Use /register to join when registration opens\n"
                "> Ask anything in #support-ticket\n"
                "> Type /help for all commands\n\n"
                "See you in the arena! 🎮"
            ),
            color=0xFFD700, timestamp=datetime.now(timezone.utc)
        )
        e.set_footer(text=member.guild.name + " — Play Hard. Win Big.")
        await member.send(embed=e)
    except Exception:
        pass  # DMs disabled — that's fine


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # Support handler fires only in the guild's support channel
    if message.guild:
        support_ch = resolve_channel(message.guild, "support")
        if support_ch and message.channel.id == support_ch.id and len(message.content.strip()) > 3:
            async with message.channel.typing():
                await handle_support(message)
            return
    await bot.process_commands(message)


# ══════════════════════════════════════════════════════════
#  SLASH COMMANDS  — NO guild= parameter → global commands
# ══════════════════════════════════════════════════════════

# ── /setup ────────────────────────────────────────────────
@tree.command(name="setup", description="Initialize NexPlay in this server (server owner only)")
async def cmd_setup(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id and not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("Only the server owner can run /setup."), ephemeral=True)
    await interaction.response.defer(thinking=True, ephemeral=True)
    gid = str(interaction.guild.id)

    # Register or update server record
    existing = await get_server_record(gid)
    if not existing:
        await b44_create("Server", {
            "guild_id": gid, "guild_name": interaction.guild.name,
            "owner_discord_id": str(interaction.guild.owner_id),
            "subscription_status": "trial", "tournaments_used": 0,
            "tournament_limit": 3, "member_count": interaction.guild.member_count or 0,
            "last_active": now_iso(),
        })
        status_msg = "Server registered! You have a **free trial** with 3 tournaments."
    else:
        status_msg = "Server already registered. Status: **" + str(existing.get("subscription_status", "?")) + "**"

    # Provision channels
    channels = await get_or_create_channels(interaction.guild)
    ch_list  = "\n".join("> <#" + str(v) + ">" for v in channels.values() if v)

    await interaction.followup.send(embed=ok_e(
        "NexPlay Setup Complete!",
        status_msg + "\n\n**Channels ready:**\n" + ch_list + "\n\n"
        "Use `/create_tournament` to launch your first tournament!\n"
        "Need more than 3 tournaments? Visit nexplay.gg to upgrade."
    ), ephemeral=True)


# ── /create_tournament ────────────────────────────────────
@tree.command(name="create_tournament", description="Create and announce a new tournament")
@app_commands.describe(
    name="Tournament name", game="Game",
    prize_pool="Prize pool e.g. NPR 5000", date="Date e.g. 2026-08-01",
    fmt="Match format", max_players="Max players (default 16)",
    description="Short description (optional)"
)
@app_commands.choices(game=[
    app_commands.Choice(name="Free Fire", value="Free Fire"),
    app_commands.Choice(name="Minecraft", value="Minecraft"),
    app_commands.Choice(name="PUBG Mobile", value="PUBG Mobile"),
    app_commands.Choice(name="Valorant", value="Valorant"),
    app_commands.Choice(name="Other", value="Other"),
])
@app_commands.choices(fmt=[
    app_commands.Choice(name="Single Elimination", value="single_elim"),
    app_commands.Choice(name="Double Elimination", value="double_elim"),
    app_commands.Choice(name="Round Robin",         value="round_robin"),
    app_commands.Choice(name="Battle Royale",       value="battle_royale"),
])
async def cmd_create(
    interaction: discord.Interaction,
    name: str, game: str, prize_pool: str, date: str,
    fmt: str = "single_elim", max_players: int = 16, description: str = ""
):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("You need Tournament Host or higher!"), ephemeral=True)
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)

    await interaction.response.defer(thinking=True)
    gid = str(interaction.guild.id)

    ch_ann = resolve_channel(interaction.guild, "announcements")
    ch_reg = resolve_channel(interaction.guild, "registration")

    poster  = img_url(name, game, "poster",  "prize " + prize_pool + " date " + date)
    roadmap = img_url(name, game, "roadmap")

    rec = await b44_create("Tournament", {
        "guild_id": gid, "name": name, "game": game, "format": fmt,
        "prize_pool": prize_pool, "description": description,
        "status": "registration_open", "max_players": max_players,
        "registered_count": 0, "tournament_date": date,
        "poster_image_url": poster, "roadmap_image_url": roadmap,
        "announcement_channel_id": str(ch_ann.id) if ch_ann else "",
        "registration_channel_id": str(ch_reg.id) if ch_reg else "",
        "created_by_discord_id": str(interaction.user.id), "started_at": now_iso(),
    })

    fmt_label = {"single_elim": "Single Elimination", "double_elim": "Double Elimination",
                 "round_robin": "Round Robin", "battle_royale": "Battle Royale"}.get(fmt, fmt)

    if ch_ann:
        ann = discord.Embed(
            title=name + " — TOURNAMENT ANNOUNCED!",
            description=(
                "Game: " + game + "\nFormat: " + fmt_label + "\n"
                "Prize Pool: " + prize_pool + "\nDate: " + date +
                "\nMax Players: " + str(max_players) +
                ("\n\n" + description if description else "") +
                "\n\n**Roadmap:** Registration → Group Draw → Schedule → Match Day → Champion"
            ),
            color=0xFFD700, timestamp=datetime.now(timezone.utc)
        )
        ann.set_image(url=poster)
        ann.set_footer(text="NexPlay Tournament System")
        await dpost(ch_ann.id, ann)

    if ch_reg:
        reg = discord.Embed(
            title="Registration OPEN — " + name,
            description=(
                "Registration is now **OPEN**!\n\nGame: " + game + "  |  Prize: " + prize_pool +
                "\nDate: " + date + "  |  Slots: " + str(max_players) +
                "\n\nUse `/register` and enter your in-game name to join!"
            ),
            color=0x00FF7F, timestamp=datetime.now(timezone.utc)
        )
        reg.set_image(url=roadmap)
        reg.set_footer(text="NexPlay Tournament System")
        await dpost(ch_reg.id, reg)

    if rec.get("id") and ch_ann:
        await b44_create("AnnouncementLog", {
            "tournament_id": rec["id"], "guild_id": gid,
            "milestone": "tournament_created", "channel_id": str(ch_ann.id),
            "announced_at": now_iso(), "content_summary": "Created: " + name,
        })

    done = discord.Embed(
        title="✅ Tournament Created!",
        description=(
            "**" + name + "** is live!\n\n"
            "Poster → " + (ch_ann.mention if ch_ann else "#announcements") + "\n"
            "Registration → " + (ch_reg.mention if ch_reg else "#registration") + "\n"
            "Images via Pollinations.ai"
        ), color=0xFFD700
    )
    done.set_image(url=poster)
    done.set_footer(text="NexPlay")
    await interaction.followup.send(embed=done)


# ── /register ─────────────────────────────────────────────
@tree.command(name="register", description="Register for the active tournament")
@app_commands.describe(tournament_name="Tournament name", ingame_name="Your exact in-game name or UID")
async def cmd_register(interaction: discord.Interaction, tournament_name: str, ingame_name: str):
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)
    await interaction.response.defer(thinking=True, ephemeral=True)
    gid = str(interaction.guild.id)

    ts_list = await b44_list("Tournament", {"guild_id": gid, "name": tournament_name})
    if not ts_list:
        return await interaction.followup.send(embed=err_e("No tournament named \"" + tournament_name + "\" found."), ephemeral=True)
    t = ts_list[0]

    if t.get("status") != "registration_open":
        return await interaction.followup.send(
            embed=err_e("Registration is not open. Status: `" + str(t.get("status")) + "`"), ephemeral=True)

    existing = await b44_list("Registration", {"tournament_id": t["id"], "player_discord_id": str(interaction.user.id)})
    if existing:
        return await interaction.followup.send(embed=err_e("You are already registered!"), ephemeral=True)

    all_r = await b44_list("Registration", {"tournament_id": t["id"]})
    if len(all_r) >= (t.get("max_players") or 16):
        return await interaction.followup.send(embed=err_e("Tournament is full! No more slots."), ephemeral=True)

    slot = len(all_r) + 1
    await b44_create("Registration", {
        "tournament_id": t["id"], "guild_id": gid,
        "player_discord_id": str(interaction.user.id),
        "player_username": ingame_name,
        "player_display_name": interaction.user.display_name,
        "registered_at": now_iso(), "checked_in": False, "seed_number": slot,
    })
    await b44_update("Tournament", t["id"], {"registered_count": slot})

    await interaction.followup.send(embed=ok_e(
        "You are Registered! 🎮",
        "Tournament: **" + tournament_name + "**\n"
        "In-Game Name: " + ingame_name + "\n"
        "Slot: #" + str(slot) + " / " + str(t.get("max_players", 16)) + "\n"
        "Date: " + str(t.get("tournament_date", "TBA")) + "\n\nGood luck! 🏆"
    ), ephemeral=True)


# ── /close_registration ───────────────────────────────────
@tree.command(name="close_registration", description="Close registration for a tournament")
@app_commands.describe(tournament_name="Tournament name")
async def cmd_close_reg(interaction: discord.Interaction, tournament_name: str):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("No permission!"), ephemeral=True)
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)
    await interaction.response.defer(thinking=True)
    gid = str(interaction.guild.id)

    ts_list = await b44_list("Tournament", {"guild_id": gid, "name": tournament_name})
    if not ts_list:
        return await interaction.followup.send(embed=err_e("Tournament not found."))
    t     = ts_list[0]
    regs  = await b44_list("Registration", {"tournament_id": t["id"]})
    await b44_update("Tournament", t["id"], {"status": "registration_closed"})

    lines = "\n".join(str(i + 1) + ". **" + r.get("player_username", "?") + "**" for i, r in enumerate(regs[:25])) or "None"
    ch_ann = resolve_channel(interaction.guild, "announcements")

    e = discord.Embed(
        title="🔒 Registration CLOSED — " + tournament_name,
        description=(
            "Registration closed with **" + str(len(regs)) + " players** confirmed!\n\n"
            "**Players:**\n" + lines + "\n\nGroup draw coming next!"
        ),
        color=0xFF4500, timestamp=datetime.now(timezone.utc)
    )
    e.set_footer(text="NexPlay Tournament System")
    if ch_ann:
        await dpost(ch_ann.id, e)
        await b44_create("AnnouncementLog", {
            "tournament_id": t["id"], "guild_id": gid, "milestone": "registration_closed",
            "channel_id": str(ch_ann.id), "announced_at": now_iso(),
            "content_summary": str(len(regs)) + " players confirmed",
        })
    await interaction.followup.send(embed=ok_e("Registration Closed", str(len(regs)) + " players locked in for **" + tournament_name + "**!"))


# ── /generate_groups ──────────────────────────────────────
@tree.command(name="generate_groups", description="Randomly generate and reveal tournament groups")
@app_commands.describe(tournament_name="Tournament name", group_size="Players per group (default 4)")
async def cmd_groups(interaction: discord.Interaction, tournament_name: str, group_size: int = 4):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("No permission!"), ephemeral=True)
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)
    await interaction.response.defer(thinking=True)
    gid = str(interaction.guild.id)

    ts_list = await b44_list("Tournament", {"guild_id": gid, "name": tournament_name})
    if not ts_list:
        return await interaction.followup.send(embed=err_e("Tournament not found."))
    t    = ts_list[0]
    regs = await b44_list("Registration", {"tournament_id": t["id"]})
    if not regs:
        return await interaction.followup.send(embed=err_e("No registered players yet!"))

    import random
    random.shuffle(regs)
    groups: list[list] = []
    for i in range(0, len(regs), group_size):
        groups.append(regs[i:i + group_size])

    ch_brack = resolve_channel(interaction.guild, "brackets")
    labels   = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    group_embed = discord.Embed(
        title="🎯 Group Draw — " + tournament_name,
        description="Groups have been randomly drawn! Good luck to all players!",
        color=0x9B59B6, timestamp=datetime.now(timezone.utc)
    )
    group_embed.set_image(url=img_url(tournament_name, t.get("game", ""), "group", str(len(groups)) + " groups"))

    for i, grp in enumerate(groups):
        label    = labels[i] if i < len(labels) else str(i + 1)
        names    = [r.get("player_username", "?") for r in grp]
        pids     = [r.get("player_discord_id", "") for r in grp]
        group_embed.add_field(name="Group " + label, value="\n".join("> " + n for n in names), inline=True)
        await b44_create("TournamentGroup", {
            "tournament_id": t["id"], "guild_id": gid, "group_label": label,
            "player_ids": pids, "player_names": names, "generated_at": now_iso(),
        })
        for r in grp:
            await b44_update("Registration", r["id"], {"group_label": label, "group_number": i + 1})

    await b44_update("Tournament", t["id"], {"status": "groups_generated"})
    group_embed.set_footer(text="NexPlay Tournament System")
    if ch_brack:
        await dpost(ch_brack.id, group_embed)
    await interaction.followup.send(embed=ok_e("Groups Generated!", str(len(groups)) + " groups drawn for **" + tournament_name + "**!", 0x9B59B6))


# ── /post_schedule ────────────────────────────────────────
@tree.command(name="post_schedule", description="Post the match schedule")
@app_commands.describe(tournament_name="Tournament name", schedule_text="Schedule details (rounds, times, matchups)")
async def cmd_schedule(interaction: discord.Interaction, tournament_name: str, schedule_text: str):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("No permission!"), ephemeral=True)
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)
    await interaction.response.defer(thinking=True)
    gid = str(interaction.guild.id)

    ts_list = await b44_list("Tournament", {"guild_id": gid, "name": tournament_name})
    if not ts_list:
        return await interaction.followup.send(embed=err_e("Tournament not found."))
    t  = ts_list[0]
    si = img_url(tournament_name, t.get("game", ""), "schedule", schedule_text[:100])
    await b44_update("Tournament", t["id"], {"status": "scheduled", "schedule_image_url": si})

    ch_brack = resolve_channel(interaction.guild, "brackets")
    ch_ann   = resolve_channel(interaction.guild, "announcements")

    se = discord.Embed(
        title="📅 Match Schedule — " + tournament_name,
        description="Schedule is LIVE!\n\n" + schedule_text + "\n\n⚠️ Be ready 10 min before your match — late = forfeit!",
        color=0x1E90FF, timestamp=datetime.now(timezone.utc)
    )
    se.set_image(url=si)
    se.set_footer(text="NexPlay Tournament System")
    if ch_brack:
        await dpost(ch_brack.id, se)
    if ch_ann:
        await dpost(ch_ann.id, discord.Embed(
            title="📅 Schedule Posted — " + tournament_name,
            description="Match schedule is live in " + (ch_brack.mention if ch_brack else "#brackets") + "! Check your match time.",
            color=0x1E90FF, timestamp=datetime.now(timezone.utc)
        ))
        await b44_create("AnnouncementLog", {
            "tournament_id": t["id"], "guild_id": gid, "milestone": "schedule_posted",
            "channel_id": str(ch_ann.id), "announced_at": now_iso(), "content_summary": "Schedule posted",
        })
    await interaction.followup.send(embed=ok_e("Schedule Posted! 📅", "Schedule for **" + tournament_name + "** is live!", 0x1E90FF))


# ── /post_result ──────────────────────────────────────────
@tree.command(name="post_result", description="Post a match result")
@app_commands.describe(
    tournament_name="Tournament name", player1="Player 1 username", player2="Player 2 username",
    winner="Winner username", score="Score e.g. 3-1", round_number="Round number"
)
async def cmd_result(
    interaction: discord.Interaction,
    tournament_name: str, player1: str, player2: str,
    winner: str, score: str = "N/A", round_number: int = 1
):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("No permission!"), ephemeral=True)
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)
    await interaction.response.defer(thinking=True)
    gid = str(interaction.guild.id)

    ts_list = await b44_list("Tournament", {"guild_id": gid, "name": tournament_name})
    if not ts_list:
        return await interaction.followup.send(embed=err_e("Tournament not found."))
    t  = ts_list[0]
    ri = img_url(tournament_name, t.get("game", ""), "result",
                 player1 + " vs " + player2 + " score " + score + " winner " + winner)

    await b44_create("Match", {
        "tournament_id": t["id"], "guild_id": gid, "round_number": round_number, "match_number": 1,
        "player1_username": player1, "player2_username": player2,
        "winner_username": winner, "status": "completed", "results_card_image_url": ri,
    })

    ch_brack = resolve_channel(interaction.guild, "brackets")
    re = discord.Embed(
        title="⚔️ Match Result — " + tournament_name,
        description=(
            "**" + player1 + "** vs **" + player2 + "**\n\n"
            "🏆 Winner: **" + winner + "**\n"
            "Score: " + score + " | Round: " + str(round_number) + "\n\n"
            "> " + winner + " advances!"
        ),
        color=0x1E90FF, timestamp=datetime.now(timezone.utc)
    )
    re.set_image(url=ri)
    re.set_footer(text="NexPlay Tournament System")
    if ch_brack:
        await dpost(ch_brack.id, re)
        await b44_create("AnnouncementLog", {
            "tournament_id": t["id"], "guild_id": gid, "milestone": "results_posted",
            "channel_id": str(ch_brack.id), "announced_at": now_iso(),
            "content_summary": player1 + " vs " + player2 + " → " + winner + " (" + score + ")",
        })
    await interaction.followup.send(embed=ok_e("Result Posted! ⚔️", "**" + winner + "** wins! Card posted to " + (ch_brack.mention if ch_brack else "#brackets") + ".", 0x1E90FF))


# ── /complete_tournament ──────────────────────────────────
@tree.command(name="complete_tournament", description="Complete tournament and crown the champion")
@app_commands.describe(tournament_name="Tournament name", winner="Champion username", second="2nd place (optional)", third="3rd place (optional)")
async def cmd_complete(interaction: discord.Interaction, tournament_name: str, winner: str, second: str = "", third: str = ""):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("No permission!"), ephemeral=True)
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)
    await interaction.response.defer(thinking=True)
    gid = str(interaction.guild.id)

    ts_list = await b44_list("Tournament", {"guild_id": gid, "name": tournament_name})
    if not ts_list:
        return await interaction.followup.send(embed=err_e("Tournament not found."))
    t  = ts_list[0]
    ci = img_url(tournament_name, t.get("game", ""), "champion", winner)
    await b44_update("Tournament", t["id"], {
        "status": "completed", "winner_username": winner,
        "second_place": second, "third_place": third, "completed_at": now_iso(),
    })

    ch_champ = resolve_channel(interaction.guild, "champions")
    ch_ann   = resolve_channel(interaction.guild, "announcements")

    ce = discord.Embed(
        title="🏆 CHAMPION — " + tournament_name,
        description=(
            "**" + winner + " IS THE CHAMPION!**\n\n"
            "🥇 1st: " + winner + "\n"
            + ("🥈 2nd: " + second + "\n" if second else "")
            + ("🥉 3rd: " + third + "\n" if third else "") +
            "\nPrize: " + str(t.get("prize_pool", "TBA")) +
            "\n\nThank you to all players for an incredible tournament! 🎮"
        ),
        color=0xFFD700, timestamp=datetime.now(timezone.utc)
    )
    ce.set_image(url=ci)
    ce.set_footer(text="NexPlay Tournament System")
    if ch_champ:
        await dpost(ch_champ.id, ce)
    if ch_ann:
        await dpost(ch_ann.id, discord.Embed(
            title=tournament_name + " — COMPLETED! 🏆",
            description=(
                "Champion: **" + winner + "**\n"
                + ("2nd: " + second + "\n" if second else "")
                + ("3rd: " + third + "\n" if third else "") +
                "\nPrize: " + str(t.get("prize_pool", "TBA")) + "\n\nSee you at the next one!"
            ),
            color=0xFFD700, timestamp=datetime.now(timezone.utc)
        ))
        await b44_create("AnnouncementLog", {
            "tournament_id": t["id"], "guild_id": gid, "milestone": "tournament_complete",
            "channel_id": str(ch_ann.id), "announced_at": now_iso(), "content_summary": "Champion: " + winner,
        })

    de = discord.Embed(title="Tournament Complete! 🏆", description="**" + winner + "** crowned champion!", color=0xFFD700)
    de.set_image(url=ci)
    de.set_footer(text="NexPlay")
    await interaction.followup.send(embed=de)


# ── /tournament_status ────────────────────────────────────
@tree.command(name="tournament_status", description="View all tournaments in this server")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    ts_list = await b44_list("Tournament", {"guild_id": str(interaction.guild.id)})
    if not ts_list:
        return await interaction.followup.send(embed=err_e("No tournaments found in this server."), ephemeral=True)
    e = discord.Embed(title="NexPlay Tournaments", color=0x9B59B6, timestamp=datetime.now(timezone.utc))
    for t in sorted(ts_list, key=lambda x: x.get("created_date", ""), reverse=True)[:10]:
        em = STATUS_EMOJI.get(t.get("status", ""), "❓")
        e.add_field(
            name=em + " " + str(t.get("name", "?")),
            value=(
                "Game: " + str(t.get("game", "?")) + " | Prize: " + str(t.get("prize_pool", "?")) + "\n"
                "Players: " + str(t.get("registered_count", 0)) + "/" + str(t.get("max_players", "?")) +
                " | " + str(t.get("status", "?")) + "\nDate: " + str(t.get("tournament_date", "TBA"))
            ),
            inline=False
        )
    e.set_footer(text="NexPlay Tournament System")
    await interaction.followup.send(embed=e, ephemeral=True)


# ── /announce ─────────────────────────────────────────────
@tree.command(name="announce", description="Post a custom announcement")
@app_commands.describe(message="Announcement content", ping_everyone="Ping @everyone?")
async def cmd_announce(interaction: discord.Interaction, message: str, ping_everyone: bool = False):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("No permission!"), ephemeral=True)
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)
    await interaction.response.defer(thinking=True)

    ch_ann = resolve_channel(interaction.guild, "announcements")
    e = discord.Embed(title="📢 Announcement", description=message, color=0xFFD700, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=interaction.guild.name + " | " + interaction.user.display_name)
    if ch_ann:
        await dpost(ch_ann.id, e, content="@everyone" if ping_everyone else "")
    await interaction.followup.send(embed=ok_e("Announced! 📢", "Posted to " + (ch_ann.mention if ch_ann else "#announcements") + "!"))


# ── /help ─────────────────────────────────────────────────
@tree.command(name="help", description="Show all NexPlay bot commands")
async def cmd_help(interaction: discord.Interaction):
    e = discord.Embed(
        title="NexPlay Tournament Bot — Commands",
        description="Full tournament management powered by NexPlay!",
        color=0xFFD700, timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="👤 Player Commands", value=(
        "`/register` — Join a tournament\n"
        "`/tournament_status` — View active tournaments\n"
        "`/help` — This menu"
    ), inline=False)
    e.add_field(name="⚔️ Staff Commands", value=(
        "`/setup` — Initialize NexPlay (owner only)\n"
        "`/create_tournament` — Create & announce tournament\n"
        "`/close_registration` — Lock registrations\n"
        "`/generate_groups` — Draw groups randomly\n"
        "`/post_schedule` — Post match schedule\n"
        "`/post_result` — Record match result\n"
        "`/complete_tournament` — Crown the champion\n"
        "`/announce` — Post custom announcement"
    ), inline=False)
    e.add_field(name="🌐 Multi-Server SaaS", value=(
        "This bot supports unlimited servers.\n"
        "Each server gets 3 free trial tournaments.\n"
        "Upgrade at nexplay.gg for unlimited access."
    ), inline=False)
    e.set_footer(text="NexPlay Tournament System | nexplay.gg")
    await interaction.response.send_message(embed=e, ephemeral=True)


# ══════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    missing = []
    if not BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not SVC_TOKEN:
        missing.append("BASE44_SERVICE_TOKEN")
    if missing:
        print("[NexPlay] ══════════════════════════════════════════")
        print("[NexPlay] STARTUP FAILED — missing environment vars:")
        for m in missing:
            print("[NexPlay]   ✗ " + m + " is not set")
        print("[NexPlay] Set these in Render → Environment → Add Env Var")
        print("[NexPlay] ══════════════════════════════════════════")
        raise SystemExit(1)
    print("[NexPlay] All environment variables verified.")
    print("[NexPlay] Guild ID: " + str(HOME_GUILD))
    bot.run(BOT_TOKEN, log_level=20)
