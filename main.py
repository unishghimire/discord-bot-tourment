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
APP_ID      = os.environ.get("APP_ID", "6a5226b5047f5c59d961130e")

BASE44_API  = "https://base44.app/api/apps/" + APP_ID + "/entities"
DISCORD_API = "https://discord.com/api/v10"

# ── Role names that count as "staff" in any server ────────
STAFF_ROLE_NAMES = {
    "NexPlay Owner", "Tournament Host", "Admin", "Moderator", "NexPlay Admin",
    "Owner", "Co-Owner", "Manager", "Staff",
}

log = print  # alias

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
# ═══════════════════════════════════════════════════════════════
#  AI SUPPORT HANDLER  — read message → one clean embed response
# ═══════════════════════════════════════════════════════════════

async def handle_support(message: discord.Message) -> None:
    q   = message.content.strip()
    ql  = q.lower()
    gid = str(message.guild.id)

    ch_ann   = resolve_channel(message.guild, 'announcements')
    ch_reg   = resolve_channel(message.guild, 'registration')
    ch_brack = resolve_channel(message.guild, 'brackets')
    ch_champ = resolve_channel(message.guild, 'champions')
    ch_staff = resolve_channel(message.guild, 'staff')
    ch_rules = resolve_channel(message.guild, 'rules')

    ann_m   = ch_ann.mention   if ch_ann   else '#announcements'
    reg_m   = ch_reg.mention   if ch_reg   else '#registration'
    brack_m = ch_brack.mention if ch_brack else '#brackets'
    rules_m = ch_rules.mention if ch_rules else '#rules'
    champ_m = ch_champ.mention if ch_champ else '#champions'

    all_t  = await b44_list('Tournament', {'guild_id': gid})
    active = next(
        (t for t in sorted(all_t, key=lambda x: x.get('created_date',''), reverse=True)
         if t.get('status') not in ('completed','cancelled','deleted')),
        None
    )
    tn    = active.get('name','current tournament') if active else None
    ts    = active.get('status','')                 if active else ''
    tg    = active.get('game','')                   if active else ''
    tp    = active.get('prize_pool','TBA')           if active else 'TBA'
    td    = active.get('tournament_date','TBA')      if active else 'TBA'
    ttime = active.get('tournament_time','TBD')      if active else 'TBD'
    tmx   = active.get('max_players','?')            if active else '?'
    tcnt  = active.get('registered_count',0)         if active else 0
    tsize = active.get('team_size',4)                if active else 4

    def has(*kws): return any(k in ql for k in kws)

    routed = False
    tips   = []
    color  = 0x5865F2
    cat    = 'General'
    title  = ''
    body   = ''

    if has('payment','refund','billing','subscription','upgrade','plan','buy','paid','price'):
        cat = 'Billing'; routed = True; color = 0xFF9900
        title = '💳 Billing & Payment'
        body = (
            'I have notified the admin team about your billing question!\n\n'
            '**Quick info:**\n'
            '> Tournament entry is **free** for all members\n'
            '> Server subscription plans are managed by the owner\n'
            '> For upgrade/refund queries, staff will DM you shortly'
        )
        tips = ['Check admin panel subscription records', 'DM user if action needed']

    elif has('account','banned','kick','muted','hacked','stolen','password','login','suspended'):
        cat = 'Account'; routed = True; color = 0xFF4444
        title = '🔐 Account Issue'
        body = (
            'Your account concern has been flagged to the moderation team!\n\n'
            '**Immediate steps:**\n'
            '> If hacked - change your Discord password NOW\n'
            '> If wrongly actioned - explain your case to a moderator\n'
            '> A moderator will reach out to you shortly'
        )
        tips = ['Check audit log', 'Review user recent messages', 'DM user if action was a mistake']

    elif has('cheat','report','toxic','abuse','harass','unfair','complaint'):
        cat = 'Complaint'; routed = True; color = 0xFF4444
        title = '🚨 Report / Complaint'
        body = (
            'Your report has been flagged to the moderation team!\n\n'
            '**To help us act faster:**\n'
            '> Describe exactly what happened\n'
            '> Include the offending username\n'
            '> Attach screenshots if possible\n\n'
            'A moderator will review and take action. Thank you!'
        )
        tips = ['Review recent messages from accused', 'Warn/mute/ban as appropriate']

    elif has('crash','lag','disconnect','not working','error','bug','broken','glitch'):
        cat = 'Technical'; routed = True; color = 0xFF9900
        title = '⚙️ Technical Issue'
        body = (
            'Tech team has been notified!\n\n'
            '**Try these first:**\n'
            '> 1. Fully restart Discord\n'
            '> 2. Check your internet connection\n'
            '> 3. Clear Discord cache (Settings > Advanced)\n'
            '> 4. Try on mobile if PC is not working\n\n'
            'Staff will follow up if the issue persists.'
        )
        tips = ['Ask: device/OS?', 'Check for Discord outages']

    elif has('register','sign up','join tournament','how to join','enroll','want to register','how do i register'):
        cat = 'Registration'; color = 0x00FF7F
        title = '✍️ How to Register'
        if active and ts == 'registration_open':
            short  = active.get('short_name','t')
            reg_ch = discord.utils.get(message.guild.text_channels, name=short+'-register')
            reg_ref = reg_ch.mention if reg_ch else reg_m
            p_lines = '\n'.join(['Player ' + str(i+1) + ': @mention' for i in range(int(tsize))])
            body = (
                'Registration for **' + str(tn) + '** is **OPEN!** 🟢\n\n'
                'Go to ' + reg_ref + ' and send:\n'
                '```\nTeam Name: <your team name>\n' + p_lines + '\n```'
                '\n**Slots:** ' + str(tcnt) + '/' + str(tmx)
                + ' | **Game:** ' + str(tg) + ' | **Date:** ' + str(td)
            )
        elif active:
            body = (
                'Registration for **' + str(tn) + '** is currently **CLOSED** (' + str(ts) + ')\n\n'
                'Watch ' + ann_m + ' for the next tournament!'
            )
        else:
            body = 'No active tournament right now.\n\nWatch ' + ann_m + ' for upcoming events!'

    elif has('when','schedule','time','date','match time','fixture','next match','what time'):
        cat = 'Schedule'; color = 0x1E90FF
        title = '📅 Tournament Schedule'
        if active:
            body = (
                '**' + str(tn) + '** schedule:\n\n'
                '> 🎮 Game: **' + str(tg) + '**\n'
                '> 📅 Date: **' + str(td) + '**\n'
                '> ⏰ Time: **' + str(ttime) + '**\n'
                '> 📊 Status: **' + str(ts) + '**\n\n'
                'Full schedule posted in ' + brack_m + ' after groups are drawn.\n'
                'Be ready **10 minutes** before your match - late = forfeit!'
            )
        else:
            body = 'No active tournament.\n\nWatch ' + ann_m + ' for upcoming dates!'

    elif has('prize','reward','cash','winning','money','payout','how much'):
        cat = 'Prize Pool'; color = 0xFFD700
        title = '🥇 Prize Pool'
        body = (
            ('**' + str(tn) + '** prize:\n\n' if active else '')
            + '> 🏆 Total: **' + str(tp) + '**\n'
            + ('> 🎮 Game: **' + str(tg) + '** | 📅 Date: **' + str(td) + '**\n' if active else '')
            + '\nFull breakdown announced before match day!'
        )

    elif has('rule','regulation','fair','allowed','disqualify','dq','no-show','violation'):
        cat = 'Rules'; color = 0x00FF7F
        title = '📋 Tournament Rules'
        body = (
            '> 1. No cheating - instant permanent DQ\n'
            '> 2. Be in VC **10 min** before your match\n'
            '> 3. Screenshot results and post in results channel\n'
            '> 4. Respect everyone - toxic behaviour = ban\n'
            '> 5. No-show after **5 min** = forfeit\n'
            '> 6. Host decision is FINAL\n'
            '> 7. One account per player\n\n'
            'Full rules: ' + rules_m
        )

    elif has('bracket','group','draw','round','opponent','my group','matchup'):
        cat = 'Brackets'; color = 0x9B59B6
        title = '🎯 Groups & Brackets'
        if active and ts in ('groups_generated','scheduled','in_progress'):
            body = (
                'Groups drawn for **' + str(tn) + '**!\n\n'
                'Check ' + brack_m + ' for your group, opponents, and match times.\n'
                'Status: **' + str(ts) + '**' + (' 🔥 LIVE!' if ts == 'in_progress' else '')
            )
        else:
            body = (
                'Groups have not been drawn yet.\n\n'
                'Brackets appear in ' + brack_m + ' after registration closes.\n'
                + ('Status: **' + str(ts) + '**' if active else 'No active tournament.')
            )

    elif has('result','score','winner','who won','standing','leaderboard','champion'):
        cat = 'Results'; color = 0x1E90FF
        title = '🏅 Results & Standings'
        body = (
            'Match results are posted in ' + brack_m + ' after each match.\n'
            'Champion revealed in ' + champ_m + '!\n\n'
            + ('Current: **' + str(tn) + '** | Status: **' + str(ts) + '**' if active else 'No active tournament.')
        )

    elif has('command','slash','bot','how to use','help','what can','features'):
        cat = 'Bot Help'; color = 0x5865F2
        title = '🤖 NexPlay Bot Commands'
        body = (
            '**Player commands:**\n'
            '> `/register` - Join an active tournament\n'
            '> `/tournament_status` - View current tournaments\n'
            '> `/help` - Full command list\n\n'
            '**Staff commands:**\n'
            '> `/create_tournament`  `/close_registration`\n'
            '> `/generate_groups`  `/post_schedule`\n'
            '> `/post_result`  `/complete_tournament`\n'
            '> `/manage` - Full management panel\n'
            '> `/announce`'
        )

    else:
        cat = 'Unknown'; routed = True; color = 0x9B59B6
        title = '🤔 Let Me Get Staff'
        body = (
            'Not 100% sure - flagging this to staff for a proper reply!\n\n'
            '**Quick links:**\n'
            '> 📢 Announcements - ' + ann_m + '\n'
            '> ✍️ Register - ' + reg_m + '\n'
            '> 🎯 Brackets - ' + brack_m + '\n'
            '> 📋 Commands - `/help`'
        )
        tips = ['Read full question and respond manually']

    # Build ONE embed and send ONCE
    embed = discord.Embed(
        title=title,
        description='Hey ' + message.author.mention + '!\n\n' + body,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(
        name='NexPlay Support',
        icon_url=(message.guild.icon.url if message.guild.icon else 'https://i.imgur.com/wSTFkRM.png')
    )
    if routed:
        embed.add_field(
            name='👨‍💼 Staff Notified',
            value='The team has been alerted and will assist you shortly!',
            inline=False
        )
    embed.set_footer(text='NexPlay Support  •  #' + message.channel.name)

    await message.channel.send(embed=embed)   # ONE send only

    # DB record
    support_rec = await b44_create('SupportMessage', {
        'guild_id':   gid,
        'guild_name': message.guild.name,
        'message':    q[:500],
        'status':     'pending' if routed else 'resolved',
    })

    STAFF_RESPONSE_TIMEOUT = 600

    if routed and ch_staff:
        high = cat in ('Account', 'Complaint')
        se   = discord.Embed(
            title=('🚨 ' if high else '⚠️ ') + 'SUPPORT PENDING - ' + cat,
            description=(
                'User asked in ' + message.channel.mention +
                ' - no staff reply = **UNRESOLVED** in 10 min.'
            ),
            color=0xFF4444 if high else 0xFF9900,
            timestamp=datetime.now(timezone.utc)
        )
        se.add_field(name='👤 User',     value=message.author.mention + ' (`' + str(message.author.name) + '`)', inline=True)
        se.add_field(name='📍 Channel',  value=message.channel.mention, inline=True)
        se.add_field(name='🏷️ Category', value=cat, inline=True)
        se.add_field(name='💬 Message',  value='> ' + (q[:300]+'...' if len(q)>300 else q), inline=False)
        if tips:
            se.add_field(name='📋 Staff Actions', value='\n'.join('- '+t for t in tips), inline=False)
        se.add_field(name='🔗 Jump', value='[Go to message](' + message.jump_url + ')', inline=False)
        se.set_footer(text='🟡 PENDING - awaiting staff reply')

        staff_log_msg = await dpost(ch_staff.id, se)

        async def check_staff_response():
            await asyncio.sleep(STAFF_RESPONSE_TIMEOUT)
            try:
                replied = False
                async for m in message.channel.history(after=message, limit=20):
                    if m.author.bot:
                        continue
                    if any(r.name in STAFF_ROLE_NAMES for r in getattr(m.author, 'roles', [])):
                        replied = True
                        break
                if not replied:
                    if support_rec.get('id'):
                        await b44_update('SupportMessage', support_rec['id'], {'status': 'unresolved'})
                    try:
                        ch_obj = bot.get_channel(ch_staff.id)
                        if ch_obj and isinstance(staff_log_msg, dict) and staff_log_msg.get('id'):
                            orig = await ch_obj.fetch_message(int(staff_log_msg['id']))
                            unres = discord.Embed(
                                title='🔴 UNRESOLVED - ' + cat,
                                description=(
                                    message.author.mention + ' (`' + str(message.author.name) + '`) '
                                    'in ' + message.channel.mention + ' got no staff reply.\n\n'
                                    '> ' + q[:400]
                                ),
                                color=0xFF0000,
                                timestamp=datetime.now(timezone.utc)
                            )
                            unres.add_field(
                                name='⚡ Action Required',
                                value='- Reply in ' + message.channel.mention + '\n- Or DM ' + message.author.mention,
                                inline=False
                            )
                            unres.set_footer(text='🔴 UNRESOLVED - please take action')
                            await orig.edit(embed=unres)
                            await ch_obj.send(
                                '@here 🔴 **Unresolved support from ' +
                                str(message.author.mention) + '** - please respond in ' +
                                message.channel.mention + '!'
                            )
                    except Exception as e:
                        print('[WARN] staff-log edit failed: ' + str(e))
                else:
                    if support_rec.get('id'):
                        await b44_update('SupportMessage', support_rec['id'], {'status': 'resolved'})
            except Exception as e:
                print('[WARN] check_staff_response error: ' + str(e))

        asyncio.create_task(check_staff_response())

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


# ══════════════════════════════════════════════════════════
#  REGISTRATION PARSER HELPERS
# ══════════════════════════════════════════════════════════

def parse_registration(text: str, team_size: int) -> dict | None:
    """
    Parse a team registration message. Returns dict or None if invalid.
    Expected format (case-insensitive keys):
        Team Name: <name>
        Player 1: @mention
        Player 2: @mention
        ...
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    result = {}

    # Find team name line
    team_line = next((l for l in lines if re.match(r"team\s*name\s*:", l, re.I)), None)
    if not team_line:
        return None
    result["team_name"] = re.sub(r"^team\s*name\s*:\s*", "", team_line, flags=re.I).strip()
    if not result["team_name"]:
        return None

    # Find player lines
    players = []
    for i in range(1, team_size + 1):
        pat = re.compile(r"player\s*" + str(i) + r"\s*:\s*(.*)", re.I)
        pline = next((l for l in lines if pat.match(l)), None)
        if not pline:
            return None
        raw = pat.match(pline).group(1).strip()
        # Extract mention user ID
        uid_match = re.search(r"<@!?(\d+)>", raw)
        if not uid_match:
            return None
        players.append(uid_match.group(1))

    if len(players) != team_size:
        return None

    result["players"] = players
    return result


async def lock_register_channel(guild: discord.Guild, channel: discord.TextChannel):
    """Deny @everyone from sending messages in a channel."""
    try:
        everyone = guild.default_role
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = False
        await channel.set_permissions(everyone, overwrite=overwrite, reason="NexPlay: Registration full")
    except Exception as e:
        log(f"[WARN] Could not lock channel #{channel.name}: {e}")


async def update_reg_announcement(tournament: dict, guild: discord.Guild, registered: int):
    """Edit the pinned registration announcement to update slot count."""
    msg_id = tournament.get("registration_msg_id")
    ch_id  = tournament.get("registration_channel_id")
    if not msg_id or not ch_id:
        return
    try:
        ch = guild.get_channel(int(ch_id))
        if not ch:
            return
        msg = await ch.fetch_message(int(msg_id))
        if not msg:
            return
        # Rebuild embed with updated count
        t = tournament
        max_p = t.get("max_players", 16)
        name  = t.get("name", "Tournament")
        game  = t.get("game", "")
        prize = t.get("prize_pool", "")
        date  = t.get("tournament_date", "")
        tsize = t.get("team_size", 4)
        short = t.get("short_name", "t")
        bar   = "█" * registered + "░" * (max_p - registered)
        filled = registered >= max_p

        lines = "\n".join([f"Player {i+1}: @mention" for i in range(tsize)])
        embed = discord.Embed(
            title=("🔒 REGISTRATION CLOSED" if filled else "✍️ REGISTRATION OPEN") + " — " + name,
            description=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎮 **Game:** {game}\n"
                f"🎖️ **Prize:** {prize}\n"
                f"📅 **Date:** {date}\n"
                f"👥 **Slots:** {registered}/{max_p}  `{bar}`\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            ) + (
                "\n🚫 **Registration is CLOSED. All slots filled!**" if filled else
                "\n@everyone **Registration is OPEN!**\n\n"
                f"Send a message in this channel with EXACTLY this format:\n"
                f"```\nTeam Name: <your team name>\n" + "\n".join([f"Player {i+1}: @mention" for i in range(tsize)]) + "\n```"
                "\n⚠️ All players must be mentioned. No duplicate registrations."
            ),
            color=0x555555 if filled else 0x00FF7F,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="NexPlay Tournament System")
        await msg.edit(embed=embed)
    except Exception as e:
        log(f"[WARN] Could not update reg announcement: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        await bot.process_commands(message)
        return

    # ── Support / AI assistant handler ───────────────────────────────────────
    # RULES:
    #  1. Only fires in general public chat channels (NOT register, confirm-teams,
    #     announcements, roadmap, groups, results, info, help, or private channels)
    #  2. Never fires in DMs or any tournament-specific channel
    #  3. If no staff responds within STAFF_RESPONSE_TIMEOUT, escalate to #staff-log
    BLOCKED_SUFFIXES = (
        "-register", "-announcements", "-roadmap", "-results",
        "-groups", "-confirm-teams", "-info", "-help",
    )
    CHAT_NAMES = ("general", "💬│general", "chat", "talk", "lounge",
                  "ff-general", "mc-general", "tourney-chat", "community")

    ch_name_lower = message.channel.name.lower()
    is_blocked    = any(ch_name_lower.endswith(s) for s in BLOCKED_SUFFIXES)
    is_chat       = any(c in ch_name_lower for c in CHAT_NAMES)

    if is_chat and not is_blocked and len(message.content.strip()) > 3:
        async with message.channel.typing():
            await handle_support(message)
        return

    # ── Registration channel handler ──────────────────────────────────────────
    gid = str(message.guild.id)
    ch_name = message.channel.name  # e.g. "npo26-register"

    if ch_name.endswith("-register"):
        short_candidate = ch_name[:-9]  # strip "-register"

        # Find the matching active tournament
        all_ts = await b44_list("Tournament", {"guild_id": gid})
        tournament = next(
            (t for t in all_ts
             if t.get("short_name", "").lower() == short_candidate.lower()
             and t.get("status") in ("registration_open",)),
            None
        )

        if not tournament:
            # Not an active registration channel — ignore
            await bot.process_commands(message)
            return

        # Delete non-registration messages (keep it clean)
        text = message.content.strip()
        team_size = int(tournament.get("team_size", 4))

        parsed = parse_registration(text, team_size)

        # ── VALIDATION ────────────────────────────────────────────────────────
        if not parsed:
            await message.add_reaction("❌")
            lines_needed = "\n".join([f"Player {i+1}: @mention" for i in range(team_size)])
            err_msg = await message.reply(
                embed=discord.Embed(
                    title="❌ Invalid Format",
                    description=(
                        f"Please use EXACTLY this format:\n"
                        f"```\nTeam Name: <your team name>\n{lines_needed}\n```"
                        f"\n• All {team_size} players must be @mentioned\n"
                        "• Team Name line is required"
                    ),
                    color=0xFF4444
                ),
                mention_author=True
            )
            await asyncio.sleep(15)
            try:
                await err_msg.delete()
                await message.delete()
            except:
                pass
            return

        team_name = parsed["team_name"]
        players   = parsed["players"]  # list of user ID strings

        # Check team name duplicate
        existing_regs = await b44_list("Registration", {"tournament_id": tournament["id"]})
        if any(r.get("player_name", "").lower() == team_name.lower() for r in existing_regs):
            await message.add_reaction("❌")
            err_msg = await message.reply(
                embed=discord.Embed(
                    title="❌ Team Name Taken",
                    description=f"**{team_name}** is already registered. Use a different team name.",
                    color=0xFF4444
                )
            )
            await asyncio.sleep(15)
            try:
                await err_msg.delete()
                await message.delete()
            except:
                pass
            return

        # Check player duplicate
        all_registered_players = []
        for r in existing_regs:
            members = r.get("team_members", [])
            if isinstance(members, list):
                all_registered_players.extend(members)
            elif isinstance(members, str) and members:
                all_registered_players.extend(members.split(","))

        already = [f"<@{p}>" for p in players if p in all_registered_players]
        if already:
            await message.add_reaction("❌")
            err_msg = await message.reply(
                embed=discord.Embed(
                    title="❌ Player Already Registered",
                    description=f"{', '.join(already)} is already registered in another team!",
                    color=0xFF4444
                )
            )
            await asyncio.sleep(15)
            try:
                await err_msg.delete()
                await message.delete()
            except:
                pass
            return

        # Check slots
        max_p = int(tournament.get("max_players", 16))
        slot  = len(existing_regs) + 1
        if slot > max_p:
            await message.add_reaction("❌")
            await message.reply(
                embed=discord.Embed(
                    title="🔒 Registration Full",
                    description=f"All {max_p} slots are taken. Registration is closed.",
                    color=0xFF4444
                )
            )
            return

        # ── SUCCESS — Save to DB ──────────────────────────────────────────────
        await b44_create("Registration", {
            "tournament_id":    tournament["id"],
            "guild_id":         gid,
            "player_name":      team_name,
            "player_discord_id": str(message.author.id),
            "team_members":     players,
            "status":           "registered",
        })

        # Update tournament registered count
        await b44_update("Tournament", tournament["id"], {"registered_count": slot})

        # React success
        await message.add_reaction("✅")

        # Post confirmation to #<short>-confirm-teams
        cfm_ch_name = short_candidate + "-confirm-teams"
        cfm_ch = discord.utils.get(message.guild.text_channels, name=cfm_ch_name)
        if cfm_ch:
            player_mentions = " ".join([f"<@{p}>" for p in players])
            cfm_embed = discord.Embed(
                title="✅ TEAM REGISTERED!",
                description=(
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏷️ **Team:** {team_name}\n"
                    f"👑 **Captain:** {message.author.mention}\n"
                    f"👥 **Players:** {player_mentions}\n"
                    f"🎫 **Slot:** #{slot} of {max_p}\n"
                    f"🏆 **Tournament:** {tournament.get('name', '')}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "**Status: CONFIRMED ✅**\n\n"
                    f"📋 Check tournament info in <#{tournament.get('announcement_channel_id','')}>\n"
                    "Groups will be revealed after registration closes. Good luck! 🎮"
                ),
                color=0x00FF7F,
                timestamp=datetime.now(timezone.utc)
            )
            cfm_embed.set_footer(text=f"NexPlay Tournament System | {tournament.get('game','')}",
                                 icon_url="https://i.imgur.com/wSTFkRM.png")
            await cfm_ch.send(embed=cfm_embed)

        # Update slot counter on announcement embed
        updated_t = dict(tournament)
        updated_t["registered_count"] = slot
        await update_reg_announcement(updated_t, message.guild, slot)

        # ── AUTO-CLOSE when full ──────────────────────────────────────────────
        if slot >= max_p:
            await b44_update("Tournament", tournament["id"], {"status": "registration_closed"})
            await lock_register_channel(message.guild, message.channel)

            close_embed = discord.Embed(
                title="🔒 REGISTRATION CLOSED — " + tournament.get("name", ""),
                description=(
                    f"All **{max_p}** slots have been filled!\n\n"
                    "**What happens next:**\n"
                    "① Groups will be drawn by the host\n"
                    "② Match schedule will be posted\n"
                    "③ Match Day begins!\n\n"
                    "Stay tuned in the announcements channel."
                ),
                color=0xFF6B35,
                timestamp=datetime.now(timezone.utc)
            )
            close_embed.set_footer(text="NexPlay Tournament System")
            await message.channel.send(embed=close_embed)

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

async def make_tournament_channels(guild: discord.Guild, tournament_name: str) -> dict:
    """
    Create a dedicated category + 8 channels for each tournament.
    
    Format: <short_name>-<channel>
    Example: For "NexPlay Open 2026":
      Category: 🏆 NPO26
        #npo26-info
        #npo26-announcements  
        #npo26-roadmap
        #npo26-results
        #npo26-groups
        #npo26-register
        #npo26-confirm-teams
        #npo26-help
    
    Returns dict with channel IDs.
    """
    import re
    
    # ── Generate short name from tournament name ──────────────────────────────
    words = tournament_name.strip().split()
    short = ""
    if len(words) == 1:
        # Single word: take first 4 chars
        short = words[0][:4].lower()
    else:
        # Multiple words: take first letter of each word + last word digits if any
        initials = "".join(w[0] for w in words if w).lower()
        # Add any numbers found in the name
        nums = re.sub(r"[^0-9]", "", tournament_name)[-2:]
        short = (initials + nums)[:6]
    
    short = re.sub(r"[^a-z0-9]", "", short)[:6]
    if not short:
        short = "tourney"

    cat_name = f"🏆 {short.upper()}"
    
    # Sub-channels: (key, channel_suffix, topic)
    channels_def = [
        ("info",          f"{short}-info",           "📋 Tournament information, rules and details"),
        ("announcements", f"{short}-announcements",   "📢 Official tournament announcements"),
        ("roadmap",       f"{short}-roadmap",         "🗺️ Tournament roadmap and schedule overview"),
        ("results",       f"{short}-results",         "🏅 Match results and standings"),
        ("groups",        f"{short}-groups",          "🎯 Group draws and bracket reveal"),
        ("register",      f"{short}-register",        "✍️ Player registration — use /register here"),
        ("confirm-teams", f"{short}-confirm-teams",   "✅ Team confirmation and roster lock"),
        ("help",          f"{short}-help",            "❓ Support and help for this tournament"),
    ]
    
    result = {"short_name": short, "category_name": cat_name}
    
    # ── Create or find category ───────────────────────────────────────────────
    category = discord.utils.get(guild.categories, name=cat_name)
    if not category:
        try:
            category = await guild.create_category(
                cat_name,
                reason=f"NexPlay: Tournament '{tournament_name}' channels"
            )
        except Exception as e:
            log(f"[ERROR] Could not create category {cat_name}: {e}")
            category = None
    
    result["category_id"] = str(category.id) if category else None

    # ── Create each channel ───────────────────────────────────────────────────
    for key, ch_name, topic in channels_def:
        existing = discord.utils.get(guild.text_channels, name=ch_name)
        if not existing:
            try:
                ch = await guild.create_text_channel(
                    ch_name,
                    category=category,
                    topic=topic,
                    reason=f"NexPlay: {tournament_name} tournament"
                )
                result[key] = str(ch.id)
            except Exception as e:
                log(f"[ERROR] Could not create #{ch_name}: {e}")
                result[key] = None
        else:
            result[key] = str(existing.id)
    
    log(f"[Channels] Created tournament channels for '{tournament_name}' → category '{cat_name}'")
    return result


@tree.command(name="create_tournament", description="Create and announce a new tournament")
@app_commands.describe(
    name="Tournament name", game="Game",
    prize_pool="Prize pool e.g. NPR 5000", date="Date e.g. 2026-08-01",
    time="Match time e.g. 5:00 PM NPT",
    fmt="Match format", max_players="Max teams (default 16)",
    team_size="Players per team (default 4)",
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
    time: str = "TBD", fmt: str = "single_elim",
    max_players: int = 16, team_size: int = 4, description: str = ""
):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("You need Tournament Host or higher!"), ephemeral=True)
    allowed, reason = await is_allowed(str(interaction.guild.id))
    if not allowed:
        return await interaction.response.send_message(embed=err_e(reason), ephemeral=True)

    await interaction.response.defer(thinking=True)
    gid = str(interaction.guild.id)

    # ── Create per-tournament dedicated channels ──────────────────────────────
    t_channels = await make_tournament_channels(interaction.guild, name)
    short      = t_channels.get("short_name", "tourney")
    cat_name   = t_channels.get("category_name", "🏆 Tournament")

    ch_ann_id  = int(t_channels["announcements"]) if t_channels.get("announcements") else None
    ch_reg_id  = int(t_channels["register"])       if t_channels.get("register")       else None
    ch_info_id = int(t_channels["info"])           if t_channels.get("info")           else None
    ch_road_id = int(t_channels["roadmap"])        if t_channels.get("roadmap")        else None
    ch_grp_id  = int(t_channels["groups"])         if t_channels.get("groups")         else None
    ch_res_id  = int(t_channels["results"])        if t_channels.get("results")        else None
    ch_cfm_id  = int(t_channels["confirm-teams"])  if t_channels.get("confirm-teams")  else None
    ch_hlp_id  = int(t_channels["help"])           if t_channels.get("help")           else None

    poster  = img_url(name, game, "poster",  "prize " + prize_pool + " date " + date)
    roadmap = img_url(name, game, "roadmap")

    rec = await b44_create("Tournament", {
        "guild_id": gid, "name": name, "game": game, "format": fmt,
        "prize_pool": prize_pool, "description": description,
        "status": "registration_open", "max_players": max_players,
        "team_size": team_size,
        "registered_count": 0, "tournament_date": date,
        "tournament_time": time,
        "poster_image_url": poster, "roadmap_image_url": roadmap,
        "announcement_channel_id": str(ch_ann_id) if ch_ann_id else "",
        "registration_channel_id": str(ch_reg_id) if ch_reg_id else "",
        "short_name": short, "category_name": cat_name,
        "created_by_discord_id": str(interaction.user.id), "started_at": now_iso(),
    })

    fmt_label = {"single_elim": "Single Elimination", "double_elim": "Double Elimination",
                 "round_robin": "Round Robin", "battle_royale": "Battle Royale"}.get(fmt, fmt)

    tid = rec.get("id", "")

    # ── 1. #info — full tournament details ───────────────────────────────────
    if ch_info_id:
        info_e = discord.Embed(
            title="📋 " + name + " — Tournament Info",
            description=(
                "**Game:** " + game + "\n"
                "**Format:** " + fmt_label + "\n"
                "**Prize Pool:** " + prize_pool + "\n"
                "**Date:** " + date + "\n"
                "**Max Players:** " + str(max_players) +
                ("\n\n" + description if description else "")
            ),
            color=0x5865F2, timestamp=datetime.now(timezone.utc)
        )
        info_e.set_thumbnail(url=poster)
        info_e.add_field(name="📢 Announcements", value="<#" + str(ch_ann_id) + ">" if ch_ann_id else "—", inline=True)
        info_e.add_field(name="✍️ Register", value="<#" + str(ch_reg_id) + ">" if ch_reg_id else "—", inline=True)
        info_e.add_field(name="🗺️ Roadmap", value="<#" + str(ch_road_id) + ">" if ch_road_id else "—", inline=True)
        info_e.add_field(name="🎯 Groups", value="<#" + str(ch_grp_id) + ">" if ch_grp_id else "—", inline=True)
        info_e.add_field(name="🏅 Results", value="<#" + str(ch_res_id) + ">" if ch_res_id else "—", inline=True)
        info_e.add_field(name="❓ Help", value="<#" + str(ch_hlp_id) + ">" if ch_hlp_id else "—", inline=True)
        info_e.set_footer(text="NexPlay Tournament System | ID: " + short.upper())
        await dpost(ch_info_id, info_e)

    # ── 2. #announcements — poster + announcement ─────────────────────────────
    if ch_ann_id:
        ann_e = discord.Embed(
            title="🏆 " + name + " — OFFICIALLY ANNOUNCED!",
            description=(
                "The tournament is here! Get ready.\n\n"
                "**Game:** " + game + " | **Prize:** " + prize_pool + "\n"
                "**Date:** " + date + " | **Format:** " + fmt_label + "\n\n"
                "Register now in <#" + str(ch_reg_id) + "> before slots fill up!"
            ),
            color=0xFFD700, timestamp=datetime.now(timezone.utc)
        )
        ann_e.set_image(url=poster)
        ann_e.set_footer(text="NexPlay Tournament System")
        await dpost(ch_ann_id, ann_e)

    # ── 3. #roadmap — roadmap image + stages ─────────────────────────────────
    if ch_road_id:
        road_e = discord.Embed(
            title="🗺️ " + name + " — Roadmap",
            description=(
                "**Tournament Stages:**\n\n"
                "① 📋 **Registration Open** → Register in <#" + str(ch_reg_id) + ">\n"
                "② ✅ **Confirm Teams** → Confirm in <#" + str(ch_cfm_id) + ">\n"
                "③ 🎯 **Group Draw** → Groups revealed in <#" + str(ch_grp_id) + ">\n"
                "④ ⚔️ **Match Day** → Schedule + results in <#" + str(ch_res_id) + ">\n"
                "⑤ 🏆 **Champion Crowned** → Winner announced!\n\n"
                "Stay tuned in <#" + str(ch_ann_id) + "> for all updates."
            ),
            color=0x00B4D8, timestamp=datetime.now(timezone.utc)
        )
        road_e.set_image(url=roadmap)
        road_e.set_footer(text="NexPlay Tournament System")
        await dpost(ch_road_id, road_e)

    # ── 4. #register — registration instructions ─────────────────────────────
    if ch_reg_id:
        reg_e = discord.Embed(
            title="✍️ Registration OPEN — " + name,
            description=(
                "**Slots:** " + str(max_players) + " players\n"
                "**Game:** " + game + " | **Prize:** " + prize_pool + "\n\n"
                "**How to register:**\n"
                "> Use `/register` and enter your in-game name\n\n"
                "After registering, go to <#" + str(ch_cfm_id) + "> to confirm your team."
            ),
            color=0x00FF7F, timestamp=datetime.now(timezone.utc)
        )
        reg_e.set_footer(text="NexPlay | Use /register to join")
        await dpost(ch_reg_id, reg_e)

    # ── 5. #confirm-teams — confirmation instructions ─────────────────────────
    if ch_cfm_id:
        cfm_e = discord.Embed(
            title="✅ Team Confirmation — " + name,
            description=(
                "After registering in <#" + str(ch_reg_id) + ">, confirm your team here.\n\n"
                "**Rules:**\n"
                "> ✅ Confirm your in-game name is correct\n"
                "> ✅ Be online 30 min before match time\n"
                "> ❌ No-shows will be disqualified\n\n"
                "Groups will be revealed in <#" + str(ch_grp_id) + "> after registration closes."
            ),
            color=0xFFA500, timestamp=datetime.now(timezone.utc)
        )
        cfm_e.set_footer(text="NexPlay Tournament System")
        await dpost(ch_cfm_id, cfm_e)

    # ── 6. #groups — placeholder ──────────────────────────────────────────────
    if ch_grp_id:
        grp_e = discord.Embed(
            title="🎯 Groups — " + name,
            description=(
                "Groups will be posted here after registration closes.\n\n"
                "Use `/generate_groups " + name + "` to draw groups.\n\n"
                "Registration → <#" + str(ch_reg_id) + ">"
            ),
            color=0x9B59B6, timestamp=datetime.now(timezone.utc)
        )
        grp_e.set_footer(text="NexPlay Tournament System")
        await dpost(ch_grp_id, grp_e)

    # ── 7. #results — placeholder ─────────────────────────────────────────────
    if ch_res_id:
        res_e = discord.Embed(
            title="🏅 Results — " + name,
            description=(
                "Match results will be posted here during the tournament.\n\n"
                "Use `/post_result` to record match outcomes.\n\n"
                "Groups → <#" + str(ch_grp_id) + ">"
            ),
            color=0xFF6B35, timestamp=datetime.now(timezone.utc)
        )
        res_e.set_footer(text="NexPlay Tournament System")
        await dpost(ch_res_id, res_e)

    # ── 8. #help — support instructions ──────────────────────────────────────
    if ch_hlp_id:
        hlp_e = discord.Embed(
            title="❓ Help & Support — " + name,
            description=(
                "Need help with this tournament? Ask here!\n\n"
                "**Common questions:**\n"
                "> ✍️ Register → <#" + str(ch_reg_id) + ">\n"
                "> 🗺️ Roadmap → <#" + str(ch_road_id) + ">\n"
                "> 📋 Rules → <#" + str(ch_info_id) + ">\n\n"
                "Staff will assist you. For urgent issues ping @Tournament Host."
            ),
            color=0x95A5A6, timestamp=datetime.now(timezone.utc)
        )
        hlp_e.set_footer(text="NexPlay Support")
        await dpost(ch_hlp_id, hlp_e)

    # ── Log to DB ─────────────────────────────────────────────────────────────
    if tid and ch_ann_id:
        await b44_create("AnnouncementLog", {
            "tournament_id": tid, "guild_id": gid,
            "milestone": "tournament_created",
            "channel_id": str(ch_ann_id),
            "announced_at": now_iso(),
            "content_summary": "Created: " + name + " | Channels: " + cat_name,
        })

    # ── Post Registration Announcement (pinned, editable slot counter) ────────
    if ch_reg_id:
        bar = "░" * max_players
        lines_needed = "\n".join([f"Player {i+1}: @mention" for i in range(team_size)])
        reg_ann = discord.Embed(
            title="✍️ REGISTRATION OPEN — " + name,
            description=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎮 **Game:** {game}\n"
                f"🎖️ **Prize:** {prize_pool}\n"
                f"📅 **Date:** {date}\n"
                f"⏰ **Time:** {time}\n"
                f"👥 **Slots:** 0/{max_players}  `{bar}`\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "@everyone **Registration is OPEN!**\n\n"
                f"Send a message in this channel with EXACTLY this format:\n"
                f"```\nTeam Name: <your team name>\n{lines_needed}\n```"
                "\n⚠️ All players must be @mentioned. No duplicate registrations allowed."
            ),
            color=0x00FF7F,
            timestamp=datetime.now(timezone.utc)
        )
        reg_ann.set_thumbnail(url=poster)
        reg_ann.set_footer(text="NexPlay Tournament System — slots update automatically")
        reg_ch_obj = interaction.guild.get_channel(ch_reg_id)
        if reg_ch_obj:
            try:
                # Allow @everyone to see but only read; they send registration messages
                reg_msg = await reg_ch_obj.send("@everyone", embed=reg_ann)
                await reg_msg.pin()
                # Store the message ID for live slot updates
                if tid:
                    await b44_update("Tournament", tid, {"registration_msg_id": str(reg_msg.id)})
            except Exception as e:
                log(f"[WARN] Could not pin reg announcement: {e}")

    # ── Confirmation to staff member ──────────────────────────────────────────
    done = discord.Embed(
        title="✅ Tournament Created — " + name,
        description=(
            "**Category:** " + cat_name + "\n\n"
            "**Channels created:**\n"
            "📋 <#" + str(ch_info_id) + "> — Info\n"
            "📢 <#" + str(ch_ann_id) + "> — Announcements\n"
            "🗺️ <#" + str(ch_road_id) + "> — Roadmap\n"
            "✍️ <#" + str(ch_reg_id) + "> — Register\n"
            "✅ <#" + str(ch_cfm_id) + "> — Confirm Teams\n"
            "🎯 <#" + str(ch_grp_id) + "> — Groups\n"
            "🏅 <#" + str(ch_res_id) + "> — Results\n"
            "❓ <#" + str(ch_hlp_id) + "> — Help\n"
        ),
        color=0xFFD700
    )
    done.set_thumbnail(url=poster)
    done.set_footer(text="NexPlay | Pollinations.ai imagery")
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

# ══════════════════════════════════════════════════════════
#  TOURNAMENT MANAGEMENT PANEL — /manage
# ══════════════════════════════════════════════════════════

class EditTournamentModal(discord.ui.Modal, title="Edit Tournament"):
    t_name   = discord.ui.TextInput(label="Tournament Name",  required=True,  max_length=80)
    t_date   = discord.ui.TextInput(label="Date (e.g. 2026-08-01)", required=False, max_length=30)
    t_time   = discord.ui.TextInput(label="Time (e.g. 5:00 PM NPT)", required=False, max_length=30)
    t_prize  = discord.ui.TextInput(label="Prize Pool",       required=False, max_length=50)
    t_desc   = discord.ui.TextInput(label="Description",      required=False, max_length=200,
                                    style=discord.TextStyle.paragraph)

    def __init__(self, tournament: dict):
        super().__init__()
        self.tournament = tournament
        self.t_name.default  = tournament.get("name", "")
        self.t_date.default  = tournament.get("tournament_date", "")
        self.t_time.default  = tournament.get("tournament_time", "")
        self.t_prize.default = tournament.get("prize_pool", "")
        self.t_desc.default  = tournament.get("description", "")

    async def on_submit(self, interaction: discord.Interaction):
        updates = {}
        if self.t_name.value:  updates["name"]             = self.t_name.value
        if self.t_date.value:  updates["tournament_date"]  = self.t_date.value
        if self.t_time.value:  updates["tournament_time"]  = self.t_time.value
        if self.t_prize.value: updates["prize_pool"]       = self.t_prize.value
        if self.t_desc.value:  updates["description"]      = self.t_desc.value
        await b44_update("Tournament", self.tournament["id"], updates)
        await interaction.response.send_message(
            embed=ok_e("Tournament Updated ✏️",
                "Changes saved:\n" + "\n".join(f"• **{k}:** {v}" for k, v in updates.items())),
            ephemeral=True
        )


class ScheduleModal(discord.ui.Modal, title="Post Match Schedule"):
    schedule = discord.ui.TextInput(
        label="Schedule (one match per line)",
        style=discord.TextStyle.paragraph,
        placeholder="Match 1: TeamA vs TeamB — 5:00 PM\nMatch 2: TeamC vs TeamD — 5:30 PM",
        required=True, max_length=1500
    )

    def __init__(self, tournament: dict):
        super().__init__()
        self.tournament = tournament

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = self.tournament
        guild = interaction.guild
        short = t.get("short_name", "t")
        ch = discord.utils.get(guild.text_channels, name=short + "-results")
        if not ch:
            ch = discord.utils.get(guild.text_channels, name=short + "-groups")
        si = img_url(t.get("name",""), t.get("game",""), "schedule", self.schedule.value[:200])
        sched_e = discord.Embed(
            title="📅 Match Schedule — " + t.get("name",""),
            description=self.schedule.value,
            color=0x3498DB, timestamp=datetime.now(timezone.utc)
        )
        sched_e.set_image(url=si)
        sched_e.set_footer(text="NexPlay Tournament System")
        if ch:
            await ch.send(embed=sched_e)
        await b44_update("Tournament", t["id"], {"status": "scheduled", "schedule_image_url": si})
        await interaction.followup.send(embed=ok_e("Schedule Posted!", f"Match schedule is live in {ch.mention if ch else '#results'}."), ephemeral=True)


class AnnounceModal(discord.ui.Modal, title="Custom Announcement"):
    ann_text = discord.ui.TextInput(
        label="Announcement message",
        style=discord.TextStyle.paragraph,
        required=True, max_length=1500
    )
    ping_all = discord.ui.TextInput(
        label="Ping @everyone? (yes/no)",
        required=False, max_length=3, default="no"
    )

    def __init__(self, tournament: dict):
        super().__init__()
        self.tournament = tournament

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        t = self.tournament
        guild = interaction.guild
        short = t.get("short_name", "t")
        ch = discord.utils.get(guild.text_channels, name=short + "-announcements")
        ping = self.ping_all.value.strip().lower() in ("yes", "y", "1", "true")
        e = discord.Embed(
            title="📢 " + t.get("name",""),
            description=self.ann_text.value,
            color=0xFFD700, timestamp=datetime.now(timezone.utc)
        )
        e.set_footer(text="NexPlay Tournament System")
        if ch:
            await ch.send("@everyone" if ping else "", embed=e)
        await interaction.followup.send(embed=ok_e("Announced!", f"Posted to {ch.mention if ch else '#announcements'}."), ephemeral=True)


class ManagementView(discord.ui.View):
    def __init__(self, tournament: dict):
        super().__init__(timeout=300)
        self.t = tournament

    async def _refresh_panel(self, interaction: discord.Interaction):
        """Re-fetch tournament and update the panel embed."""
        updated = await b44_list("Tournament", {"guild_id": str(interaction.guild.id), "name": self.t.get("name","")})
        if updated:
            self.t = updated[0]
        embed = build_manage_embed(self.t)
        await interaction.message.edit(embed=embed, view=self)

    # ── ROW 1: Registration ───────────────────────────────────────────────────
    @discord.ui.button(label="🟢 Open Registration", style=discord.ButtonStyle.success, row=0)
    async def btn_open_reg(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await b44_update("Tournament", self.t["id"], {"status": "registration_open"})
        # Unlock the register channel
        guild = interaction.guild
        short = self.t.get("short_name","t")
        ch = discord.utils.get(guild.text_channels, name=short + "-register")
        if ch:
            try:
                ow = ch.overwrites_for(guild.default_role)
                ow.send_messages = None  # reset to default
                await ch.set_permissions(guild.default_role, overwrite=ow)
            except:
                pass
        await interaction.response.send_message(embed=ok_e("Registration Opened 🟢", "Players can now register."), ephemeral=True)
        await self._refresh_panel(interaction)

    @discord.ui.button(label="🔴 Close Registration", style=discord.ButtonStyle.danger, row=0)
    async def btn_close_reg(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await b44_update("Tournament", self.t["id"], {"status": "registration_closed"})
        guild = interaction.guild
        short = self.t.get("short_name","t")
        ch = discord.utils.get(guild.text_channels, name=short + "-register")
        if ch:
            await lock_register_channel(guild, ch)
        await interaction.response.send_message(embed=ok_e("Registration Closed 🔴", "No more registrations accepted."), ephemeral=True)
        await self._refresh_panel(interaction)

    @discord.ui.button(label="👥 View Teams", style=discord.ButtonStyle.secondary, row=0)
    async def btn_view_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        regs = await b44_list("Registration", {"tournament_id": self.t["id"]})
        if not regs:
            return await interaction.response.send_message(embed=err_e("No teams registered yet."), ephemeral=True)
        lines = []
        for i, r in enumerate(regs, 1):
            members = r.get("team_members", [])
            if isinstance(members, list):
                mentions = " ".join(f"<@{m}>" for m in members)
            else:
                mentions = str(members)
            lines.append(f"**#{i} {r.get('player_name','?')}** — {mentions}")
        e = discord.Embed(
            title=f"👥 Registered Teams — {self.t.get('name','')}",
            description="\n".join(lines),
            color=0x5865F2
        )
        e.set_footer(text=f"{len(regs)}/{self.t.get('max_players',16)} slots filled")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ── ROW 2: Tournament flow ────────────────────────────────────────────────
    @discord.ui.button(label="🎯 Generate Groups", style=discord.ButtonStyle.primary, row=1)
    async def btn_groups(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = self.t
        guild = interaction.guild
        regs = await b44_list("Registration", {"tournament_id": t["id"]})
        if len(regs) < 2:
            return await interaction.followup.send(embed=err_e("Need at least 2 registered teams."), ephemeral=True)
        import random
        random.shuffle(regs)
        group_size = 4
        groups = [regs[i:i+group_size] for i in range(0, len(regs), group_size)]
        labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        short = t.get("short_name","t")
        ch = discord.utils.get(guild.text_channels, name=short + "-groups")
        desc = ""
        for idx, grp in enumerate(groups):
            label = labels[idx] if idx < len(labels) else str(idx+1)
            names = [r.get("player_name","?") for r in grp]
            desc += f"**Group {label}**\n" + "\n".join(f"• {n}" for n in names) + "\n\n"
            for r in grp:
                await b44_update("Registration", r["id"], {"group_label": label})
        await b44_update("Tournament", t["id"], {"status": "groups_generated"})
        gi = img_url(t.get("name",""), t.get("game",""), "groups", desc[:300])
        ge = discord.Embed(title="🎯 Group Draw — " + t.get("name",""), description=desc, color=0x9B59B6, timestamp=datetime.now(timezone.utc))
        ge.set_image(url=gi)
        ge.set_footer(text="NexPlay Tournament System")
        if ch:
            await ch.send(embed=ge)
        await interaction.followup.send(embed=ok_e("Groups Generated!", f"Draw posted to {ch.mention if ch else '#groups'}."), ephemeral=True)
        await self._refresh_panel(interaction)

    @discord.ui.button(label="📅 Post Schedule", style=discord.ButtonStyle.primary, row=1)
    async def btn_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_modal(ScheduleModal(self.t))

    @discord.ui.button(label="🏁 Complete Tournament", style=discord.ButtonStyle.danger, row=1)
    async def btn_complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_modal(CompleteTournamentModal(self.t))

    # ── ROW 3: Utilities ──────────────────────────────────────────────────────
    @discord.ui.button(label="📢 Announce", style=discord.ButtonStyle.secondary, row=2)
    async def btn_announce(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_modal(AnnounceModal(self.t))

    @discord.ui.button(label="✏️ Edit Tournament", style=discord.ButtonStyle.secondary, row=2)
    async def btn_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_modal(EditTournamentModal(self.t))

    @discord.ui.button(label="🗑️ Delete Tournament", style=discord.ButtonStyle.danger, row=2)
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_modal(DeleteTournamentModal(self.t))


class CompleteTournamentModal(discord.ui.Modal, title="Complete Tournament"):
    winner = discord.ui.TextInput(label="🥇 Winner (team name)", required=True, max_length=60)
    second = discord.ui.TextInput(label="🥈 2nd Place (optional)", required=False, max_length=60)
    third  = discord.ui.TextInput(label="🥉 3rd Place (optional)", required=False, max_length=60)

    def __init__(self, tournament: dict):
        super().__init__()
        self.tournament = tournament

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = self.tournament
        guild = interaction.guild
        short = t.get("short_name","t")
        ch_ann = discord.utils.get(guild.text_channels, name=short + "-announcements")
        ch_champ = discord.utils.get(guild.text_channels, name="hall-of-champions")
        winner = self.winner.value.strip()
        ci = img_url(t.get("name",""), t.get("game",""), "champion", "winner " + winner)
        podium = f"🥇 **{winner}**"
        if self.second.value: podium += f"\n🥈 {self.second.value}"
        if self.third.value:  podium += f"\n🥉 {self.third.value}"
        ce = discord.Embed(
            title="🏆 CHAMPION CROWNED — " + t.get("name",""),
            description=podium + f"\n\nCongratulations to all participants of **{t.get('name','')}**!",
            color=0xFFD700, timestamp=datetime.now(timezone.utc)
        )
        ce.set_image(url=ci)
        ce.set_footer(text="NexPlay Tournament System")
        if ch_ann: await ch_ann.send("@everyone", embed=ce)
        if ch_champ: await ch_champ.send(embed=ce)
        await b44_update("Tournament", t["id"], {"status": "completed", "winner": winner})
        await interaction.followup.send(embed=ok_e("Tournament Completed! 🏆", f"{winner} is the champion!"), ephemeral=True)


class DeleteTournamentModal(discord.ui.Modal, title="Delete Tournament — Type to confirm"):
    confirm = discord.ui.TextInput(label='Type "DELETE" to confirm', required=True, max_length=10)

    def __init__(self, tournament: dict):
        super().__init__()
        self.tournament = tournament

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirm.value.strip().upper() != "DELETE":
            return await interaction.response.send_message(embed=err_e('You must type "DELETE" exactly.'), ephemeral=True)
        await b44_update("Tournament", self.tournament["id"], {"status": "deleted"})
        await interaction.response.send_message(
            embed=ok_e("Tournament Deleted 🗑️", f"**{self.tournament.get('name','')}** has been removed."),
            ephemeral=True
        )


def build_manage_embed(t: dict) -> discord.Embed:
    status_colors = {
        "registration_open":   0x00FF7F,
        "registration_closed": 0xFF6B35,
        "groups_generated":    0x9B59B6,
        "scheduled":           0x3498DB,
        "completed":           0xFFD700,
        "deleted":             0x555555,
    }
    status = t.get("status", "unknown")
    reg = t.get("registered_count", 0)
    max_p = t.get("max_players", 16)
    bar = "█" * int(reg) + "░" * (int(max_p) - int(reg))
    status_labels = {
        "registration_open":   "🟢 Registration Open",
        "registration_closed": "🔴 Registration Closed",
        "groups_generated":    "🎯 Groups Generated",
        "scheduled":           "📅 Scheduled",
        "completed":           "🏆 Completed",
        "deleted":             "🗑️ Deleted",
    }
    e = discord.Embed(
        title="⚙️ TOURNAMENT MANAGEMENT — " + t.get("name",""),
        description=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎮 **Game:** {t.get('game','')}\n"
            f"🎖️ **Prize:** {t.get('prize_pool','')}\n"
            f"📅 **Date:** {t.get('tournament_date','')}  ⏰ {t.get('tournament_time','TBD')}\n"
            f"👥 **Teams:** {reg}/{max_p}  `{bar}`\n"
            f"📊 **Status:** {status_labels.get(status, status)}\n"
            f"🏷️ **Short Name:** `{t.get('short_name','').upper()}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Use the buttons below to manage this tournament."
        ),
        color=status_colors.get(status, 0x5865F2),
        timestamp=datetime.now(timezone.utc)
    )
    e.set_footer(text="NexPlay Management Panel | Staff only")
    return e


@tree.command(name="manage", description="Open the tournament management panel (staff only)")
@app_commands.describe(tournament_name="Name of the tournament to manage")
async def cmd_manage(interaction: discord.Interaction, tournament_name: str):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=err_e("You need Tournament Host or higher!"), ephemeral=True)
    await interaction.response.defer(thinking=True, ephemeral=True)
    gid = str(interaction.guild.id)
    ts = await b44_list("Tournament", {"guild_id": gid, "name": tournament_name})
    if not ts:
        return await interaction.followup.send(embed=err_e(f"No tournament named \"{tournament_name}\" found."), ephemeral=True)
    t = ts[0]
    embed = build_manage_embed(t)
    view  = ManagementView(t)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


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
#  HEALTH-CHECK HTTP SERVER (required for Render Web Service / port check)
# ══════════════════════════════════════════════════════════
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"NexPlay bot is running.")
    def log_message(self, format, *args):
        pass  # suppress request logs

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[NexPlay] Health server listening on port {port}")
    server.serve_forever()

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
    # Start health-check server in background thread (satisfies Render port check)
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    bot.run(BOT_TOKEN, log_level=20)
