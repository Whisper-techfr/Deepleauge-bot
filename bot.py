import discord
from discord import app_commands
from discord.ext import commands, tasks
import database as db
import asyncio
import os
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
WAGER_LOBBY_CHANNEL_ID = 0   # Put the channel ID where /wager_start embed lives
GUILD_LEADER_ROLE_NAME = "Guild Leader"
GUILD_CHANNEL_NAME = "guilds"

# How many seconds between each background-task tick (1 hour = 3600)
TASK_INTERVAL_SECONDS = 3600

# How many days of silence before the bot pings players to fight
FIGHT_PING_DAYS = 3

# How many hours of silence (after no fight-ping has been sent yet, or after a
# ping was sent but nobody responded) before the close-warning appears
INACTIVITY_CLOSE_HOURS = 72   # 3 days

# How many hours the close-warning button is available before auto-close
EXTENSION_HOURS = 48
# ──────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND TASK — ticket activity monitor
# ═══════════════════════════════════════════════════════════════════════════════

@tasks.loop(seconds=TASK_INTERVAL_SECONDS)
async def ticket_activity_task():
    """
    Every hour this task scans every active ticket channel and:

    1. WAR + WAGER (LIVE only): if no fight-ping has ever been sent, or the last
       fight-ping was sent ≥ FIGHT_PING_DAYS days ago, ping all participants and
       tell them to schedule their match.

    2. WAGER only: if the last user message was ≥ INACTIVITY_CLOSE_HOURS ago
       AND no close-warning is currently active, post a close-warning embed with
       a 'Keep Open' button and record the expiry time.

    3. WAGER only: if a close-warning IS active and its expiry has passed, delete
       the channel and cancel the wager.
    """
    now = datetime.now(timezone.utc)
    tickets = await db.get_all_active_tickets()

    for row in tickets:
        channel_id  = row["channel_id"]
        ticket_type = row["ticket_type"]   # 'wager' or 'war'
        ticket_id   = row["ticket_id"]

        channel = bot.get_channel(channel_id)
        if channel is None:
            # Channel no longer exists — clean up DB
            await db.delete_ticket_activity(channel_id)
            continue

        # ── Verify the ticket is still in an active state ────────────────────
        if ticket_type == "wager":
            wager = await db.get_wager(ticket_id)
            if not wager or wager["status"] not in ("pending", "open"):
                await db.delete_ticket_activity(channel_id)
                continue
            participants = await db.get_wager_players(ticket_id)
            participant_ids = [p["user_id"] for p in participants]

        elif ticket_type == "war":
            war = await db.get_war(ticket_id)
            # Only ping for wars that are LIVE (accepted)
            if not war or war["status"] != "open":
                if war and war["status"] in ("done", "cancelled"):
                    await db.delete_ticket_activity(channel_id)
                continue
            # Collect all management members of both guilds
            members_a = await db.get_guild_members(war["guild_a_id"])
            members_b = await db.get_guild_members(war["guild_b_id"])
            mgmt_roles = {"leader", "co_leader", "manager"}
            participant_ids = list({
                m["user_id"] for m in (list(members_a) + list(members_b))
                if m["role"] in mgmt_roles
            })
        else:
            continue

        # ── Parse timestamps from DB ─────────────────────────────────────────
        def parse_ts(val):
            if not val:
                return None
            try:
                dt = datetime.fromisoformat(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None

        last_msg_at       = parse_ts(row["last_message_at"])
        last_ping_at      = parse_ts(row["last_fight_ping_at"])
        ext_expires_at    = parse_ts(row["extension_expires_at"])
        ext_offered_at    = parse_ts(row["extension_offered_at"])

        # ── Step 1: Auto-close if extension window has expired (wager only) ──
        if ticket_type == "wager" and ext_expires_at and now >= ext_expires_at:
            try:
                await channel.send(
                    "⏰ **No response received.** This ticket has been automatically closed due to inactivity."
                )
                await asyncio.sleep(3)
                await channel.delete(reason="Wager auto-closed: inactivity timeout expired")
            except Exception:
                pass
            await db.cancel_wager(ticket_id)
            await db.delete_ticket_activity(channel_id)
            continue

        # ── Step 2: Fight ping every FIGHT_PING_DAYS days ────────────────────
        # Only ping when nobody has been pinged yet, or the last ping was
        # sent more than FIGHT_PING_DAYS days ago.
        ping_due = False
        if last_ping_at is None:
            # First ping: send it FIGHT_PING_DAYS after ticket was registered
            if last_msg_at and (now - last_msg_at).days >= FIGHT_PING_DAYS:
                ping_due = True
        else:
            if (now - last_ping_at).days >= FIGHT_PING_DAYS:
                ping_due = True

        # Don't send a fight ping while a close-warning is active
        if ext_offered_at:
            ping_due = False

        if ping_due:
            mentions = " ".join(f"<@{uid}>" for uid in participant_ids)
            try:
                label = "wager" if ticket_type == "wager" else "war"
                await channel.send(
                    f"⏰ **Reminder** — {mentions}\n"
                    f"It's been **{FIGHT_PING_DAYS} days** since this {label} ticket was last active. "
                    f"Please schedule your match and get it done! ⚔️"
                )
                await db.set_fight_ping_sent(channel_id)
            except Exception:
                pass

        # ── Step 3: Wager inactivity close-warning ───────────────────────────
        # Only for wagers that are LIVE (all players have accepted).
        if ticket_type == "wager":
            wager = await db.get_wager(ticket_id)
            if wager and wager["status"] == "open" and not ext_offered_at:
                inactive_hours = (now - last_msg_at).total_seconds() / 3600 if last_msg_at else 0
                if inactive_hours >= INACTIVITY_CLOSE_HOURS:
                    expires_at = now + timedelta(hours=EXTENSION_HOURS)
                    expires_iso = expires_at.isoformat()
                    await db.set_extension_offered(channel_id, expires_iso)
                    view = WagerExtensionView(ticket_id)
                    ts = int(expires_at.timestamp())
                    try:
                        await channel.send(
                            f"⚠️ **This wager ticket has been inactive for {FIGHT_PING_DAYS} days.**\n\n"
                            f"If nobody clicks **Keep Open** by <t:{ts}:F> (<t:{ts}:R>), "
                            f"this ticket will be **automatically closed**.",
                            view=view
                        )
                    except Exception:
                        pass


@ticket_activity_task.before_loop
async def before_ticket_task():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════════
#  WAGER EXTENSION VIEW  (Keep Open button)
# ═══════════════════════════════════════════════════════════════════════════════

class WagerExtensionView(discord.ui.View):
    """
    Posted when a wager ticket has been inactive for ≥ 3 days.
    Any participant can click 'Keep Open' to reset the inactivity clock.
    If nobody clicks within 48 hours the background task deletes the channel.
    """

    def __init__(self, wager_id: int = 0):
        super().__init__(timeout=None)
        self.wager_id = wager_id

    def _resolve_wager_id(self, interaction: discord.Interaction):
        try:
            parts = interaction.channel.name.split("-")
            return int(parts[1])
        except (IndexError, ValueError):
            return self.wager_id or None

    @discord.ui.button(
        label="✅ Keep Open",
        style=discord.ButtonStyle.success,
        custom_id="wager_keep_open"
    )
    async def keep_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        wager_id = self._resolve_wager_id(interaction)
        if wager_id is None:
            await interaction.response.send_message("❌ Could not resolve wager.", ephemeral=True)
            return

        players = await db.get_wager_players(wager_id)
        player_ids = [p["user_id"] for p in players]
        if interaction.user.id not in player_ids:
            await interaction.response.send_message("❌ You're not part of this wager.", ephemeral=True)
            return

        await db.clear_extension_offer(interaction.channel.id)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} kept the ticket open. Inactivity timer has been reset."
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  MESSAGE LISTENER — track activity in ticket channels
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    """Update last_message_at whenever a real user posts in a ticket channel."""
    if message.author.bot:
        await bot.process_commands(message)
        return

    # Only update if this channel is a tracked ticket
    row = await db.get_ticket_activity(message.channel.id)
    if row:
        await db.update_ticket_last_message(message.channel.id)

    await bot.process_commands(message)


# ═══════════════════════════════════════════════════════════════════════════════
#  WAGER SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

class WagerStartView(discord.ui.View):
    """Persistent view posted by /wager_start — lives in lobby channel forever."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚔️ Commence 1v1", style=discord.ButtonStyle.primary, custom_id="wager_1v1")
    async def commence_1v1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(Wager1v1Modal())

    @discord.ui.button(label="🌊 Commence 2v2", style=discord.ButtonStyle.primary, custom_id="wager_2v2")
    async def commence_2v2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(Wager2v2Modal())


# ── AUTOCOMPLETE HELPERS ──────────────────────────────────────────────────────

async def member_autocomplete(interaction: discord.Interaction, current: str):
    """Returns server members matching the typed string."""
    if not current:
        members = list(interaction.guild.members)[:25]
    else:
        current_lower = current.lower()
        members = [
            m for m in interaction.guild.members
            if current_lower in m.display_name.lower() or current_lower in m.name.lower()
        ][:25]
    return [
        app_commands.Choice(name=f"{m.display_name} ({m.name})", value=str(m.id))
        for m in members
        if not m.bot
    ]


# ── WAGER SLASH COMMANDS ──────────────────────────────────────────────────────

@bot.tree.command(name="wager_1v1", description="Challenge someone to a 1v1 wager")
@app_commands.describe(opponent="The member you want to wager")
@app_commands.autocomplete(opponent=member_autocomplete)
async def wager_1v1_cmd(interaction: discord.Interaction, opponent: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    if not opponent.isdigit():
        await interaction.followup.send("❌ Invalid selection.", ephemeral=True)
        return

    opp_member = guild.get_member(int(opponent))
    if not opp_member:
        await interaction.followup.send("❌ Could not find that member.", ephemeral=True)
        return

    if opp_member.id == interaction.user.id:
        await interaction.followup.send("❌ You can't wager yourself!", ephemeral=True)
        return

    await create_wager_ticket(interaction, "1v1", [interaction.user], [opp_member])


@bot.tree.command(name="wager_2v2", description="Start a 2v2 wager")
@app_commands.describe(
    teammate="Your teammate",
    opponent1="Opponent 1",
    opponent2="Opponent 2",
)
@app_commands.autocomplete(
    teammate=member_autocomplete,
    opponent1=member_autocomplete,
    opponent2=member_autocomplete,
)
async def wager_2v2_cmd(
    interaction: discord.Interaction,
    teammate: str,
    opponent1: str,
    opponent2: str,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    members = []
    for val in [teammate, opponent1, opponent2]:
        if not val.isdigit():
            await interaction.followup.send(f"❌ Invalid selection: {val}", ephemeral=True)
            return
        m = guild.get_member(int(val))
        if not m:
            await interaction.followup.send(f"❌ Could not find member with ID {val}", ephemeral=True)
            return
        members.append(m)

    tm, opp1, opp2 = members
    await create_wager_ticket(interaction, "2v2", [interaction.user, tm], [opp1, opp2])


class Wager1v1Modal(discord.ui.Modal, title="Create 1v1 Wager"):
    opponent = discord.ui.TextInput(
        label="Opponent",
        placeholder="Enter their @mention or user ID",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        raw = self.opponent.value.strip().strip("<@!>")
        if not raw.isdigit():
            await interaction.followup.send("❌ Invalid opponent. Use their @mention or user ID.", ephemeral=True)
            return
        opponent = guild.get_member(int(raw))
        if not opponent:
            await interaction.followup.send("❌ Could not find that member in this server.", ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.followup.send("❌ You can't wager yourself!", ephemeral=True)
            return
        await create_wager_ticket(interaction, "1v1", [interaction.user], [opponent])


class Wager2v2Modal(discord.ui.Modal, title="Create 2v2 Wager"):
    teammate = discord.ui.TextInput(label="Your Teammate", placeholder="@mention or user ID", required=True)
    opponent1 = discord.ui.TextInput(label="Opponent 1", placeholder="@mention or user ID", required=True)
    opponent2 = discord.ui.TextInput(label="Opponent 2", placeholder="@mention or user ID", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        members = []
        for val in [self.teammate.value, self.opponent1.value, self.opponent2.value]:
            raw = val.strip().strip("<@!>")
            if not raw.isdigit():
                await interaction.followup.send(f"❌ Invalid mention: {val}", ephemeral=True)
                return
            m = guild.get_member(int(raw))
            if not m:
                await interaction.followup.send(f"❌ Could not find member: {val}", ephemeral=True)
                return
            members.append(m)
        teammate, opp1, opp2 = members
        await create_wager_ticket(interaction, "2v2", [interaction.user, teammate], [opp1, opp2])


async def create_wager_ticket(interaction: discord.Interaction, type_: str, team_a: list, team_b: list):
    guild = interaction.guild

    wager_id = await db.create_wager(type_, interaction.user.id)
    for m in team_a:
        accepted = 1 if m.id == interaction.user.id else 0
        await db.add_wager_player(wager_id, m.id, "A", accepted)
    for m in team_b:
        await db.add_wager_player(wager_id, m.id, "B", 0)

    all_players = team_a + team_b

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    for m in all_players:
        overwrites[m] = discord.PermissionOverwrite(read_messages=True, send_messages=False)

    ticket_channel = await guild.create_text_channel(
        name=f"wager-{wager_id}-{type_}",
        overwrites=overwrites,
        reason=f"Wager #{wager_id}"
    )
    await db.set_wager_ticket_channel(wager_id, ticket_channel.id)

    # Start tracking activity for this wager ticket
    await db.register_ticket_activity(ticket_channel.id, "wager", wager_id)

    embed = build_wager_embed(wager_id, type_, team_a, team_b, "PENDING")
    view = WagerTicketView(wager_id)

    await ticket_channel.send(
        content=" ".join(m.mention for m in all_players),
        embed=embed,
        view=view
    )
    await interaction.followup.send(f"✅ Ticket created! {ticket_channel.mention}", ephemeral=True)


def build_wager_embed(wager_id, type_, team_a, team_b, status):
    color = 0xF59E0B if status == "PENDING" else 0x22C55E if status == "LIVE" else 0x6B7280
    embed = discord.Embed(title=f"⚔️ Wager #{wager_id} — {type_}", color=color)

    def fmt(members):
        return "\n".join(f"<@{m.id if hasattr(m, 'id') else m}>" for m in members) or "—"

    embed.add_field(name="🔵 Team A", value=fmt(team_a), inline=True)
    embed.add_field(name="🔴 Team B", value=fmt(team_b), inline=True)
    embed.add_field(name="📊 Status", value=status, inline=False)
    embed.set_footer(text=f"Wager #{wager_id} • Deepleague")
    return embed


class WagerTicketView(discord.ui.View):
    """
    Persistent — all state from DB.
    custom_id encodes the wager_id so it survives bot restarts.
    """

    def __init__(self, wager_id: int = 0):
        super().__init__(timeout=None)
        self.wager_id = wager_id

    def _resolve_wager_id(self, interaction: discord.Interaction):
        """Parse wager ID from channel name like 'wager-<id>-<type>'."""
        try:
            parts = interaction.channel.name.split("-")
            return int(parts[1])
        except (IndexError, ValueError):
            return None

    @discord.ui.button(label="✅ Accept Ticket", style=discord.ButtonStyle.success, custom_id="waccept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        wager_id = self._resolve_wager_id(interaction)
        if wager_id is None:
            await interaction.response.send_message("❌ Could not resolve wager from this channel.", ephemeral=True)
            return

        players = await db.get_wager_players(wager_id)
        player_ids = [p["user_id"] for p in players]

        if interaction.user.id not in player_ids:
            await interaction.response.send_message("❌ You're not part of this wager.", ephemeral=True)
            return

        wager = await db.get_wager(wager_id)
        if wager["status"] != "pending":
            await interaction.response.send_message("❌ This wager is no longer pending.", ephemeral=True)
            return

        all_accepted = await db.accept_wager_player(wager_id, interaction.user.id)

        if all_accepted:
            for m_id in player_ids:
                member = interaction.guild.get_member(m_id)
                if member:
                    await interaction.channel.set_permissions(member, read_messages=True, send_messages=True)

            wager = await db.get_wager(wager_id)
            players = await db.get_wager_players(wager_id)
            team_a = [p["user_id"] for p in players if p["team"] == "A"]
            team_b = [p["user_id"] for p in players if p["team"] == "B"]
            embed = build_wager_embed(wager_id, wager["type"], team_a, team_b, "LIVE")

            for item in self.children:
                item.disabled = True
            await interaction.message.edit(embed=embed, view=self)
            await interaction.response.send_message("✅ All players accepted — wager is **LIVE**! Discuss here. 🔱")
        else:
            await interaction.response.send_message(f"✅ {interaction.user.mention} accepted. Waiting for others.")

    @discord.ui.button(label="❌ Dodge", style=discord.ButtonStyle.danger, custom_id="wdodge")
    async def dodge(self, interaction: discord.Interaction, button: discord.ui.Button):
        wager_id = self._resolve_wager_id(interaction)
        if wager_id is None:
            await interaction.response.send_message("❌ Could not resolve wager from this channel.", ephemeral=True)
            return

        players = await db.get_wager_players(wager_id)
        player_ids = [p["user_id"] for p in players]

        if interaction.user.id not in player_ids:
            await interaction.response.send_message("❌ You're not part of this wager.", ephemeral=True)
            return

        wager = await db.get_wager(wager_id)
        if wager["status"] != "pending":
            await interaction.response.send_message("❌ This wager is no longer pending.", ephemeral=True)
            return

        await db.cancel_wager(wager_id)
        await db.delete_ticket_activity(interaction.channel.id)
        await interaction.response.send_message(f"❌ {interaction.user.mention} dodged the wager. Ticket closing in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()


@bot.tree.command(name="wager_start", description="Post the wager start panel (Admins only)")
async def wager_start(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    embed = discord.Embed(
        title="🌊 Deepleague Wager System",
        description="Ready to wager? Use **/wager_1v1** or **/wager_2v2** for full member autocomplete!\n\n"
                    "Or click below to use the classic panel:\n\n"
                    "⚔️ **1v1 Duel** — You vs one opponent\n"
                    "🌊 **2v2** — You + teammate vs two opponents",
        color=0x1455A4
    )
    embed.set_footer(text="Deepleague • Wager System")
    await interaction.response.send_message(embed=embed, view=WagerStartView())


@bot.tree.command(name="wager_result", description="Declare wager winner (Admins only)")
@app_commands.describe(wager_id="The wager ticket number", winning_team="Which team won")
@app_commands.choices(winning_team=[
    app_commands.Choice(name="Team A", value="A"),
    app_commands.Choice(name="Team B", value="B"),
])
async def wager_result(interaction: discord.Interaction, wager_id: int, winning_team: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    wager = await db.get_wager(wager_id)
    if not wager:
        await interaction.response.send_message("❌ Wager not found.", ephemeral=True)
        return
    await db.set_wager_result(wager_id, winning_team)
    # Clean up activity tracking now that the wager is done
    if wager["ticket_channel_id"]:
        await db.delete_ticket_activity(wager["ticket_channel_id"])
    await interaction.response.send_message(f"🏆 **Team {winning_team}** wins Wager #{wager_id}!")


@bot.tree.command(name="wager_leaderboard", description="Top players by wager wins")
async def wager_leaderboard(interaction: discord.Interaction):
    rows = await db.get_wager_leaderboard(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("No completed wagers yet.", ephemeral=True)
        return
    embed = discord.Embed(title="🏆 Wager Leaderboard", color=0xF59E0B)
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else f"#{i+1}"
        member = interaction.guild.get_member(row["user_id"])
        name = member.display_name if member else f"User {row['user_id']}"
        embed.add_field(name=f"{medal} {name}", value=f"{row['wins']}W / {row['losses']}L", inline=False)
    await interaction.response.send_message(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  GUILD SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

async def build_guild_embed(guild_data, members):
    def get_role(role_name):
        lines = []
        for m in members:
            if m["role"] == role_name:
                tick = "✅" if m.get("accepted", 1) else "⏳"
                lines.append(f"{tick} <@{m['user_id']}>")
        return "\n".join(lines) or "—"

    desc = guild_data.get("description") or ""
    embed = discord.Embed(title=f"🔱 {guild_data['name']}", description=desc, color=0x1455A4)
    embed.add_field(name="👑 Leader", value=f"✅ <@{guild_data['leader_id']}>", inline=True)
    co = guild_data.get("co_leader_id")
    embed.add_field(name="🥈 Co-Leader", value=f"<@{co}>" if co else "—", inline=True)
    embed.add_field(name="⚙️ Managers", value=get_role("manager"), inline=False)
    embed.add_field(name="⚔️ Main Roster", value=get_role("main_roster"), inline=True)
    embed.add_field(name="🔄 Sub Roster", value=get_role("sub_roster"), inline=True)
    return embed


async def update_guild_forum(bot_instance, discord_guild, guild_data):
    members = await db.get_guild_members(guild_data["id"])
    embed = await build_guild_embed(dict(guild_data), [dict(m) for m in members])
    channel = discord.utils.get(discord_guild.text_channels, name=GUILD_CHANNEL_NAME)
    if channel:
        msg_id = guild_data.get("forum_message_id")
        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed)
                return
            except Exception:
                pass
        msg = await channel.send(embed=embed)
        await db.set_guild_forum_message(guild_data["id"], msg.id)


class GuildInviteView(discord.ui.View):
    """
    Persistent view. guild_id and user_id encoded in custom_id so it
    survives bot restarts. Declining does NOT set a merc cooldown.
    """

    def __init__(self, guild_id: int = 0, user_id: int = 0, guild_name: str = "", discord_guild_id: int = 0):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.guild_name = guild_name
        self.discord_guild_id = discord_guild_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success, custom_id="ginvite_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This invite is not for you.", ephemeral=True)
            return

        exempt_roles = ["head mod"]
        user_roles = [r.name.lower() for r in interaction.user.roles]
        is_exempt = any(r in user_roles for r in exempt_roles)

        if not is_exempt:
            on_cd, hours_left = await db.check_guild_cooldown(self.user_id)
            if on_cd:
                await interaction.response.send_message(
                    f"❌ You have a **merc cooldown**! You can join a guild in **{hours_left:.1f} hours**.", ephemeral=True
                )
                return

        existing_accepted = await db.get_guild_by_member_accepted(self.user_id, self.discord_guild_id)
        if existing_accepted:
            await interaction.response.send_message(
                f"❌ You are already in **{existing_accepted['name']}**. Leave first.", ephemeral=True
            )
            return

        await db.accept_guild_member(self.guild_id, self.user_id)
        guild_data = await db.get_guild(self.guild_id)
        discord_guild = bot.get_guild(self.discord_guild_id)
        if discord_guild and guild_data:
            await update_guild_forum(bot, discord_guild, guild_data)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"✅ You joined **{self.guild_name}**!")

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger, custom_id="ginvite_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This invite is not for you.", ephemeral=True)
            return

        await db.remove_guild_member(self.guild_id, self.user_id)

        guild_data = await db.get_guild(self.guild_id)
        discord_guild = bot.get_guild(self.discord_guild_id)
        if discord_guild and guild_data:
            await update_guild_forum(bot, discord_guild, guild_data)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"❌ You declined the invite to **{self.guild_name}**.")


@bot.tree.command(name="register_guild", description="Register a guild (Mods only)")
@app_commands.describe(
    name="Guild name",
    leader="The guild leader",
    co_leader="The co-leader",
    manager1="Manager 1",
    manager2="Manager 2 (optional)",
    main1="Main roster player 1",
    main2="Main roster player 2",
    main3="Main roster player 3",
    main4="Main roster player 4 (optional)",
    main5="Main roster player 5 (optional)",
    sub1="Sub roster player 1 (optional)",
    sub2="Sub roster player 2 (optional)",
    sub3="Sub roster player 3 (optional)",
    sub4="Sub roster player 4 (optional)",
    sub5="Sub roster player 5 (optional)",
)
async def register_guild(
    interaction: discord.Interaction,
    name: str,
    leader: discord.Member,
    co_leader: discord.Member,
    manager1: discord.Member,
    main1: discord.Member,
    main2: discord.Member,
    main3: discord.Member,
    manager2: discord.Member = None,
    main4: discord.Member = None,
    main5: discord.Member = None,
    sub1: discord.Member = None,
    sub2: discord.Member = None,
    sub3: discord.Member = None,
    sub4: discord.Member = None,
    sub5: discord.Member = None,
):
    await interaction.response.defer(ephemeral=True)

    # Only mods/admins can register guilds
    if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Only moderators and admins can register guilds.", ephemeral=True)
        return

    guild_id = await db.register_guild(name, interaction.guild_id, leader.id)
    if guild_id is None:
        await interaction.followup.send("❌ A guild with that name already exists.", ephemeral=True)
        return

    role_map = {}
    role_map[co_leader] = ("co_leader", "Co-Leader")
    role_map[manager1] = ("manager", "Manager")
    if manager2:
        role_map[manager2] = ("manager", "Manager")
    for m in [main1, main2, main3, main4, main5]:
        if m:
            role_map[m] = ("main_roster", "Main Roster")
    for m in [sub1, sub2, sub3, sub4, sub5]:
        if m:
            role_map[m] = ("sub_roster", "Sub Roster")

    await db.add_guild_member(guild_id, co_leader.id, "co_leader")
    await db.set_guild_co_leader(guild_id, co_leader.id)
    for member, (role_key, _) in role_map.items():
        if member and member != co_leader and member.id != leader.id:
            await db.add_guild_member(guild_id, member.id, role_key)

    role = discord.utils.get(interaction.guild.roles, name=GUILD_LEADER_ROLE_NAME)
    if role:
        try:
            await leader.add_roles(role)
        except Exception:
            pass

    members_rows = await db.get_guild_members(guild_id)
    guild_data = await db.get_guild(guild_id)
    embed = await build_guild_embed(dict(guild_data), [dict(m) for m in members_rows])
    guild_channel = discord.utils.get(interaction.guild.text_channels, name=GUILD_CHANNEL_NAME)
    if guild_channel:
        msg = await guild_channel.send(embed=embed)
        await db.set_guild_forum_message(guild_id, msg.id)
    else:
        await interaction.channel.send(embed=embed)

    await interaction.followup.send("✅ Guild registered! Sending invites to members...", ephemeral=True)

    async def send_invites():
        for member, (role_key, role_label) in role_map.items():
            if member and member.id != interaction.user.id:
                try:
                    view = GuildInviteView(guild_id, member.id, name, interaction.guild_id)
                    await member.send(
                        f"🔱 **You have been invited to {name} as {role_label}!**\n\n"
                        f"Guild Leader: <@{interaction.user.id}>\n\n"
                        f"Accept or decline below:",
                        view=view
                    )
                except discord.Forbidden:
                    pass

    asyncio.ensure_future(send_invites())


@bot.tree.command(name="leave_guild", description="Leave your current guild")
async def leave_guild(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    my_guild = await db.get_guild_by_member(interaction.user.id, interaction.guild_id)
    if not my_guild:
        await interaction.followup.send("❌ You are not in a guild.", ephemeral=True)
        return

    guild_members = await db.get_guild_members(my_guild["id"])
    my_row = next((m for m in guild_members if m["user_id"] == interaction.user.id), None)

    if my_row and my_row["role"] == "leader":
        await interaction.followup.send("❌ You are the Guild Leader — transfer leadership or disband first.", ephemeral=True)
        return

    await db.remove_guild_member(my_guild["id"], interaction.user.id)

    if my_row and my_row["accepted"] == 1:
        await db.set_guild_cooldown(interaction.user.id)
        await interaction.followup.send(
            f"✅ You have left **{my_guild['name']}**. You have a **48-hour merc cooldown** before joining another guild.",
            ephemeral=True
        )
    else:
        await interaction.followup.send(f"✅ Invite to **{my_guild['name']}** cancelled.", ephemeral=True)

    guild_data = await db.get_guild(my_guild["id"])
    if guild_data:
        await update_guild_forum(bot, interaction.guild, guild_data)


@bot.tree.command(name="disband_guild", description="Disband your guild (Guild Leader only)")
async def disband_guild(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    my_guild = await db.get_guild_by_leader(interaction.user.id, interaction.guild_id)
    if not my_guild:
        await interaction.followup.send("❌ You are not a Guild Leader.", ephemeral=True)
        return

    guild_name = my_guild["name"]
    members = await db.get_guild_members(my_guild["id"])

    await db.disband_guild(my_guild["id"])

    role = discord.utils.get(interaction.guild.roles, name=GUILD_LEADER_ROLE_NAME)
    if role:
        try:
            await interaction.user.remove_roles(role)
        except Exception:
            pass

    for m in members:
        if m["user_id"] == interaction.user.id:
            continue
        if m["accepted"] == 1:
            await db.set_guild_cooldown(m["user_id"])
            member = interaction.guild.get_member(m["user_id"])
            if member:
                try:
                    await member.send(
                        f"💔 **{guild_name} has been disbanded** by the Guild Leader.\n"
                        f"You are no longer part of this guild. You have a 48-hour merc cooldown."
                    )
                except discord.Forbidden:
                    pass
        else:
            member = interaction.guild.get_member(m["user_id"])
            if member:
                try:
                    await member.send(f"ℹ️ Your pending invite to **{guild_name}** has been cancelled (guild disbanded).")
                except discord.Forbidden:
                    pass

    await interaction.followup.send(f"🗑️ **{guild_name}** has been disbanded.", ephemeral=False)


# ── GUILD PANEL ──────────────────────────────────────────────────────────────

class GuildPanelView(discord.ui.View):
    def __init__(self, guild_id: int, kicker_role: str):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.kicker_role = kicker_role

    @discord.ui.button(label="👢 Kick Member", style=discord.ButtonStyle.danger, row=0)
    async def kick_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KickMemberModal(self.guild_id, self.kicker_role))

    @discord.ui.button(label="➕ Add Member", style=discord.ButtonStyle.success, row=0)
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.kicker_role not in ["leader", "co_leader", "manager"]:
            await interaction.response.send_message("❌ Only Leader, Co-Leader or Manager can add members.", ephemeral=True)
            return
        await interaction.response.send_modal(AddMemberModal(self.guild_id, self.kicker_role))

    @discord.ui.button(label="📝 Set Description", style=discord.ButtonStyle.secondary, row=1)
    async def set_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.kicker_role not in ["leader", "co_leader"]:
            await interaction.response.send_message("❌ Only the Leader or Co-Leader can set the description.", ephemeral=True)
            return
        await interaction.response.send_modal(SetDescriptionModal(self.guild_id))

    @discord.ui.button(label="👁️ View Roster", style=discord.ButtonStyle.primary, row=1)
    async def view_roster(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_data = await db.get_guild(self.guild_id)
        members = await db.get_guild_members(self.guild_id)
        embed = await build_guild_embed(dict(guild_data), [dict(m) for m in members])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="👑 Transfer Leader", style=discord.ButtonStyle.secondary, row=2)
    async def transfer_leader(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.kicker_role != "leader":
            await interaction.response.send_message("❌ Only the Guild Leader can transfer leadership.", ephemeral=True)
            return
        await interaction.response.send_modal(TransferLeaderModal(self.guild_id))


class AddMemberModal(discord.ui.Modal, title="Add a Guild Member"):
    member_id = discord.ui.TextInput(label="Member to add", placeholder="Enter their @mention or user ID", required=True)
    role = discord.ui.TextInput(label="Role", placeholder="main_roster or sub_roster", required=True)

    def __init__(self, guild_id, adder_role):
        super().__init__()
        self.guild_id = guild_id
        self.adder_role = adder_role

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw = self.member_id.value.strip().strip("<@!>")
        if not raw.isdigit():
            await interaction.followup.send("❌ Invalid mention. Use @mention or user ID.", ephemeral=True)
            return
        target = interaction.guild.get_member(int(raw))
        if not target:
            await interaction.followup.send("❌ Member not found.", ephemeral=True)
            return
        role_input = self.role.value.strip().lower().replace(" ", "_")
        valid_roles = ["main_roster", "sub_roster"]
        if self.adder_role in ["leader", "co_leader"]:
            valid_roles += ["manager", "co_leader"]
        if role_input not in valid_roles:
            await interaction.followup.send(f"❌ Invalid role. Valid options: {', '.join(valid_roles)}", ephemeral=True)
            return
        guild_data = await db.get_guild(self.guild_id)
        success, msg = await db.add_guild_member(self.guild_id, target.id, role_input)
        if not success:
            await interaction.followup.send(f"❌ {msg}", ephemeral=True)
            return
        await update_guild_forum(bot, interaction.guild, guild_data)
        try:
            view = GuildInviteView(self.guild_id, target.id, guild_data["name"], interaction.guild_id)
            await target.send(
                f"🔱 **You have been invited to {guild_data['name']} as {role_input.replace('_', ' ').title()}!**\n\n"
                f"Accept or decline below:",
                view=view
            )
        except discord.Forbidden:
            pass
        await interaction.followup.send(f"✅ Invite sent to {target.mention} as **{role_input.replace('_', ' ').title()}**.")


class TransferLeaderModal(discord.ui.Modal, title="Transfer Guild Leadership"):
    member_id = discord.ui.TextInput(label="New Leader", placeholder="Enter their @mention or user ID", required=True)

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw = self.member_id.value.strip().strip("<@!>")
        if not raw.isdigit():
            await interaction.followup.send("❌ Invalid mention. Use @mention or user ID.", ephemeral=True)
            return
        target = interaction.guild.get_member(int(raw))
        if not target:
            await interaction.followup.send("❌ Member not found.", ephemeral=True)
            return
        if target.id == interaction.user.id:
            await interaction.followup.send("❌ You are already the leader.", ephemeral=True)
            return
        await db.transfer_guild_leader(self.guild_id, target.id, interaction.user.id)
        guild_data = await db.get_guild(self.guild_id)
        role = discord.utils.get(interaction.guild.roles, name=GUILD_LEADER_ROLE_NAME)
        if role:
            try:
                await interaction.user.remove_roles(role)
                await target.add_roles(role)
            except Exception:
                pass
        await update_guild_forum(bot, interaction.guild, guild_data)
        try:
            await target.send(
                f"👑 **You are now the Guild Leader of {guild_data['name']}!**\n"
                f"Leadership was transferred to you by <@{interaction.user.id}>."
            )
        except discord.Forbidden:
            pass
        await interaction.followup.send(f"👑 Leadership of **{guild_data['name']}** transferred to {target.mention}!")


class KickMemberModal(discord.ui.Modal, title="Kick a Guild Member"):
    member_id = discord.ui.TextInput(label="Member to kick", placeholder="Enter their @mention or user ID", required=True)

    def __init__(self, guild_id, kicker_role):
        super().__init__()
        self.guild_id = guild_id
        self.kicker_role = kicker_role

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw = self.member_id.value.strip().strip("<@!>")
        if not raw.isdigit():
            await interaction.followup.send("❌ Invalid mention. Use @mention or user ID.", ephemeral=True)
            return
        target = interaction.guild.get_member(int(raw))
        if not target:
            await interaction.followup.send("❌ Member not found.", ephemeral=True)
            return
        guild_members = await db.get_guild_members(self.guild_id)
        target_row = next((m for m in guild_members if m["user_id"] == target.id), None)
        if not target_row:
            await interaction.followup.send("❌ That person is not in your guild.", ephemeral=True)
            return
        target_role = target_row["role"]
        if target.id == interaction.user.id:
            await interaction.followup.send("❌ You can't kick yourself.", ephemeral=True)
            return
        kickable = {
            "leader":    ["co_leader", "manager", "main_roster", "sub_roster"],
            "co_leader": ["manager", "main_roster", "sub_roster"],
            "manager":   ["main_roster", "sub_roster"],
        }
        if target_role not in kickable.get(self.kicker_role, []):
            await interaction.followup.send(f"❌ You can't kick a **{target_role.replace('_', ' ').title()}**.", ephemeral=True)
            return
        guild_data = await db.get_guild(self.guild_id)
        await db.remove_guild_member(self.guild_id, target.id)
        if target_row["accepted"] == 1:
            await db.set_guild_cooldown(target.id)
            cooldown_msg = " They now have a 48-hour merc cooldown."
            dm_msg = f"❌ **You have been kicked from {guild_data['name']}** by <@{interaction.user.id}>. You have a 48-hour merc cooldown before joining another guild."
        else:
            cooldown_msg = ""
            dm_msg = f"ℹ️ Your pending invite to **{guild_data['name']}** has been cancelled."
        await update_guild_forum(bot, interaction.guild, guild_data)
        try:
            await target.send(dm_msg)
        except discord.Forbidden:
            pass
        await interaction.followup.send(f"✅ {target.mention} has been removed from **{guild_data['name']}**.{cooldown_msg}")


class SetDescriptionModal(discord.ui.Modal, title="Set Guild Description"):
    description = discord.ui.TextInput(
        label="Guild Description",
        placeholder="Write something about your guild...",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=True
    )

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await db.set_guild_description(self.guild_id, self.description.value)
        guild_data = await db.get_guild(self.guild_id)
        await update_guild_forum(bot, interaction.guild, guild_data)
        await interaction.followup.send("✅ Description updated and guild page refreshed!", ephemeral=True)


@bot.tree.command(name="guild_panel", description="Manage your guild")
async def guild_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    my_guild = await db.get_guild_by_member(interaction.user.id, interaction.guild_id)
    if not my_guild:
        await interaction.followup.send("❌ You are not in a guild.", ephemeral=True)
        return
    guild_members = await db.get_guild_members(my_guild["id"])
    my_role = next((m["role"] for m in guild_members if m["user_id"] == interaction.user.id), None)
    if not my_role:
        await interaction.followup.send("❌ You are not in a guild.", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"⚙️ Guild Panel — {my_guild['name']}",
        description=f"Your role: **{my_role.replace('_', ' ').title()}**\n\nUse the buttons below to manage your guild.",
        color=0x1455A4
    )
    view = GuildPanelView(my_guild["id"], my_role)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="guild_info", description="View a guild's roster")
@app_commands.describe(name="Guild name")
async def guild_info(interaction: discord.Interaction, name: str):
    guild_data = await db.get_guild_by_name(name, interaction.guild_id)
    if not guild_data:
        await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
        return
    members = await db.get_guild_members(guild_data["id"])
    embed = await build_guild_embed(dict(guild_data), [dict(m) for m in members])
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rename_guild", description="Rename your guild (Guild Leader only)")
@app_commands.describe(new_name="The new name for your guild")
async def rename_guild(interaction: discord.Interaction, new_name: str):
    await interaction.response.defer(ephemeral=True)
    my_guild = await db.get_guild_by_leader(interaction.user.id, interaction.guild_id)
    if not my_guild:
        await interaction.followup.send("❌ Only the Guild Leader can rename the guild.", ephemeral=True)
        return
    existing = await db.get_guild_by_name(new_name, interaction.guild_id)
    if existing:
        await interaction.followup.send(f"❌ A guild named **{new_name}** already exists.", ephemeral=True)
        return
    import aiosqlite
    async with aiosqlite.connect("deepleague.db") as db_conn:
        await db_conn.execute("UPDATE guilds SET name=? WHERE id=?", (new_name, my_guild["id"]))
        await db_conn.commit()
    guild_data = await db.get_guild(my_guild["id"])
    await update_guild_forum(bot, interaction.guild, guild_data)
    await interaction.followup.send(f"✅ Guild renamed to **{new_name}**!", ephemeral=True)


# ── ADMIN: view dodge counts ──────────────────────────────────────────────────

@bot.tree.command(name="guild_dodges", description="Check a guild's remaining dodges (Admins only)")
@app_commands.describe(guild_name="The guild name to check")
async def guild_dodges(interaction: discord.Interaction, guild_name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    guild_data = await db.get_guild_by_name(guild_name, interaction.guild_id)
    if not guild_data:
        await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
        return
    can_dodge, remaining = await db.guild_can_dodge(guild_data["id"])
    count = await db.get_guild_dodge_count(guild_data["id"])
    if can_dodge:
        msg = f"🛡️ **{guild_data['name']}** has used **{count}/{db.DODGE_LIMIT}** dodges. **{remaining} remaining.**"
    else:
        msg = f"🚫 **{guild_data['name']}** has used all **{db.DODGE_LIMIT}** dodges and can no longer dodge wars."
    await interaction.response.send_message(msg)


# ═══════════════════════════════════════════════════════════════════════════════
#  WAR SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

class WarStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚔️ Declare War", style=discord.ButtonStyle.blurple, custom_id="war_declare")
    async def declare_war_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        my_guild = await db.get_guild_by_leader(interaction.user.id, interaction.guild_id)
        if not my_guild:
            await interaction.response.send_message("❌ Only Guild Leaders can declare war.", ephemeral=True)
            return
        await interaction.response.send_modal(WarDeclareModal(my_guild["id"]))


class WarDeclareModal(discord.ui.Modal, title="Declare War"):
    target_guild = discord.ui.TextInput(
        label="Enemy Guild Name",
        placeholder="Type the exact guild name you want to war",
        required=True
    )

    def __init__(self, challenger_guild_id):
        super().__init__()
        self.challenger_guild_id = challenger_guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        target_name = self.target_guild.value.strip()
        target = await db.get_guild_by_name(target_name, interaction.guild_id)
        if not target:
            await interaction.followup.send(f"❌ Guild '{target_name}' not found.", ephemeral=True)
            return
        if target["id"] == self.challenger_guild_id:
            await interaction.followup.send("❌ You can't war your own guild.", ephemeral=True)
            return
        view = WarRegionView(self.challenger_guild_id, target["id"])
        await interaction.followup.send(f"⚔️ Challenging **{target['name']}**! Pick the region:", view=view, ephemeral=True)


@bot.tree.command(name="war_start", description="Post the war panel (Admins only)")
async def war_start(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    embed = discord.Embed(
        title="🔱 Deepleague War System",
        description="Guild Leaders — ready to go to war?\n\n"
                    "Click the button below to challenge another guild!\n\n"
                    "⚠️ You must have a registered guild to declare war.",
        color=0x1455A4
    )
    embed.set_footer(text="Deepleague • War System")
    await interaction.response.send_message(embed=embed, view=WarStartView())


class WarRegionSelect(discord.ui.Select):
    def __init__(self, challenger_guild_id, target_guild_id):
        self.challenger_guild_id = challenger_guild_id
        self.target_guild_id = target_guild_id
        options = [
            discord.SelectOption(label="🌍 Europe", value="Europe"),
            discord.SelectOption(label="🌎 NA East", value="NA East"),
            discord.SelectOption(label="🌎 NA West", value="NA West"),
        ]
        super().__init__(placeholder="Pick a region...", options=options, custom_id="war_region")

    async def callback(self, interaction: discord.Interaction):
        region = self.values[0]
        await interaction.response.defer(ephemeral=True)
        await create_war_ticket(interaction, self.challenger_guild_id, self.target_guild_id, region)
        self.view.stop()


class WarRegionView(discord.ui.View):
    def __init__(self, challenger_guild_id, target_guild_id):
        super().__init__(timeout=60)
        self.add_item(WarRegionSelect(challenger_guild_id, target_guild_id))


async def create_war_ticket(interaction: discord.Interaction, guild_a_id: int, guild_b_id: int, region: str):
    server = interaction.guild
    war_id = await db.create_war(guild_a_id, guild_b_id, region, interaction.user.id)
    guild_a = await db.get_guild(guild_a_id)
    guild_b = await db.get_guild(guild_b_id)
    members_a = await db.get_guild_members(guild_a_id)
    members_b = await db.get_guild_members(guild_b_id)

    mgmt_roles = {"leader", "co_leader", "manager"}
    mgmt_a = [m["user_id"] for m in members_a if m["role"] in mgmt_roles]
    mgmt_b = [m["user_id"] for m in members_b if m["role"] in mgmt_roles]
    mgmt_a.append(guild_a["leader_id"])
    mgmt_b.append(guild_b["leader_id"])
    all_mgmt = list(set(mgmt_a + mgmt_b))

    overwrites = {
        server.default_role: discord.PermissionOverwrite(read_messages=False),
        server.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    for uid in all_mgmt:
        member = server.get_member(uid)
        if member:
            overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=False)

    ticket_channel = await server.create_text_channel(
        name=f"war-{war_id}-{guild_a['name']}-vs-{guild_b['name']}",
        overwrites=overwrites,
        reason=f"War #{war_id}"
    )
    await db.set_war_ticket_channel(war_id, ticket_channel.id)

    # Start activity tracking for this war ticket
    await db.register_ticket_activity(ticket_channel.id, "war", war_id)

    # Check if guild_b can still dodge — show remaining count in embed
    can_dodge, dodges_remaining = await db.guild_can_dodge(guild_b_id)

    embed = discord.Embed(title=f"⚔️ War #{war_id} — {guild_a['name']} vs {guild_b['name']}", color=0xF59E0B)
    embed.add_field(name="🔵 Guild A", value=guild_a["name"], inline=True)
    embed.add_field(name="🔴 Guild B", value=guild_b["name"], inline=True)
    embed.add_field(name="🌍 Region", value=region, inline=True)
    embed.add_field(name="📊 Status", value="PENDING", inline=False)
    if can_dodge:
        embed.add_field(
            name="🛡️ Dodge Budget",
            value=f"{guild_b['name']} has **{dodges_remaining}** dodge(s) remaining out of {db.DODGE_LIMIT}.",
            inline=False
        )
    else:
        embed.add_field(
            name="🚫 No Dodges Left",
            value=f"{guild_b['name']} has used all {db.DODGE_LIMIT} dodges and **must accept** this war.",
            inline=False
        )
    embed.set_footer(text=f"War #{war_id} • Only Guild Leader or Co-Leader can accept/dodge")

    view = WarTicketView(war_id, guild_b_id)

    mentions = " ".join(f"<@{uid}>" for uid in all_mgmt)
    await ticket_channel.send(content=mentions, embed=embed, view=view)
    await interaction.followup.send(f"✅ War ticket created! {ticket_channel.mention}", ephemeral=True)


class WarTicketView(discord.ui.View):
    """
    Persistent — resolves war_id from channel name, all state from DB.
    Dodge button is disabled when the defending guild has no dodges left.
    """

    def __init__(self, war_id: int = 0, guild_b_id: int = 0):
        super().__init__(timeout=None)
        self.war_id = war_id
        self.guild_b_id = guild_b_id

    def _resolve_war_id(self, interaction: discord.Interaction):
        """Parse war ID from channel name like 'war-42-GuildA-vs-GuildB'."""
        try:
            parts = interaction.channel.name.split("-")
            return int(parts[1])
        except (IndexError, ValueError):
            return None

    @discord.ui.button(label="✅ Accept War", style=discord.ButtonStyle.success, custom_id="waraccept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        war_id = self._resolve_war_id(interaction)
        if war_id is None:
            await interaction.response.send_message("❌ Could not resolve war from this channel.", ephemeral=True)
            return

        war = await db.get_war(war_id)
        if not war:
            await interaction.response.send_message("❌ War not found.", ephemeral=True)
            return
        if war["status"] != "pending":
            await interaction.response.send_message("❌ This war is no longer pending.", ephemeral=True)
            return

        guild_b = await db.get_guild(war["guild_b_id"])
        allowed = [guild_b["leader_id"], guild_b["co_leader_id"]]
        if interaction.user.id not in allowed:
            await interaction.response.send_message("❌ Only the Guild Leader or Co-Leader of the challenged guild can accept.", ephemeral=True)
            return

        await db.accept_war(war_id)

        # Grant write permissions to both guilds' management
        members_a = await db.get_guild_members(war["guild_a_id"])
        members_b = await db.get_guild_members(war["guild_b_id"])
        mgmt_roles = {"leader", "co_leader", "manager"}
        all_mgmt_ids = {
            m["user_id"] for m in (list(members_a) + list(members_b))
            if m["role"] in mgmt_roles
        }
        for uid in all_mgmt_ids:
            member = interaction.guild.get_member(uid)
            if member:
                await interaction.channel.set_permissions(member, read_messages=True, send_messages=True)

        guild_a = await db.get_guild(war["guild_a_id"])

        embed = discord.Embed(title=f"⚔️ War #{war_id} — {guild_a['name']} vs {guild_b['name']}", color=0x22C55E)
        embed.add_field(name="🔵 Guild A", value=guild_a["name"], inline=True)
        embed.add_field(name="🔴 Guild B", value=guild_b["name"], inline=True)
        embed.add_field(name="🌍 Region", value=war["region"], inline=True)
        embed.add_field(name="📊 Status", value="LIVE 🔥", inline=False)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message("✅ War accepted — **IT'S ON!** 🔱")

    @discord.ui.button(label="❌ Dodge", style=discord.ButtonStyle.danger, custom_id="wardodge")
    async def dodge(self, interaction: discord.Interaction, button: discord.ui.Button):
        war_id = self._resolve_war_id(interaction)
        if war_id is None:
            await interaction.response.send_message("❌ Could not resolve war from this channel.", ephemeral=True)
            return

        war = await db.get_war(war_id)
        if not war:
            await interaction.response.send_message("❌ War not found.", ephemeral=True)
            return
        if war["status"] != "pending":
            await interaction.response.send_message("❌ This war is no longer pending.", ephemeral=True)
            return

        guild_b = await db.get_guild(war["guild_b_id"])
        allowed = [guild_b["leader_id"], guild_b["co_leader_id"]]
        if interaction.user.id not in allowed:
            await interaction.response.send_message("❌ Only the Guild Leader or Co-Leader can dodge.", ephemeral=True)
            return

        # ── DODGE LIMIT CHECK ────────────────────────────────────────────────
        can_dodge, dodges_remaining = await db.guild_can_dodge(war["guild_b_id"])
        if not can_dodge:
            await interaction.response.send_message(
                f"🚫 **{guild_b['name']}** has already used all **{db.DODGE_LIMIT}** dodges. "
                f"You are **forced to accept** this war — click ✅ Accept War.",
                ephemeral=True
            )
            return

        # Consume one dodge
        new_count = await db.increment_guild_dodge(war["guild_b_id"])
        remaining_after = db.DODGE_LIMIT - new_count

        await db.cancel_war(war_id)
        await db.delete_ticket_activity(interaction.channel.id)

        dodge_info = (
            f"\n\n🛡️ **{guild_b['name']}** has used **{new_count}/{db.DODGE_LIMIT}** dodges. "
            + (f"**{remaining_after} remaining.**" if remaining_after > 0 else "**No more dodges left — all future wars must be accepted!**")
        )

        await interaction.response.send_message(
            f"❌ {interaction.user.mention} dodged the war. Ticket closing in 5 seconds...{dodge_info}"
        )
        await asyncio.sleep(5)
        await interaction.channel.delete()


@bot.tree.command(name="declare_war", description="Challenge another guild to a war (Guild Leaders only)")
@app_commands.describe(target_guild="Name of the guild you want to war")
async def declare_war(interaction: discord.Interaction, target_guild: str):
    await interaction.response.defer(ephemeral=True)
    my_guild = await db.get_guild_by_leader(interaction.user.id, interaction.guild_id)
    if not my_guild:
        await interaction.followup.send("❌ You must be a Guild Leader to declare war.", ephemeral=True)
        return
    target = await db.get_guild_by_name(target_guild, interaction.guild_id)
    if not target:
        await interaction.followup.send(f"❌ Guild '{target_guild}' not found.", ephemeral=True)
        return
    if target["id"] == my_guild["id"]:
        await interaction.followup.send("❌ You can't war your own guild.", ephemeral=True)
        return
    view = WarRegionView(my_guild["id"], target["id"])
    await interaction.followup.send(f"⚔️ Challenging **{target['name']}** to a war! Pick the region:", view=view, ephemeral=True)


@bot.tree.command(name="war_result", description="Declare war winner (Admins only)")
@app_commands.describe(war_id="The war ticket number", winning_guild="Name of the winning guild")
async def war_result(interaction: discord.Interaction, war_id: int, winning_guild: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    guild_data = await db.get_guild_by_name(winning_guild, interaction.guild_id)
    if not guild_data:
        await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
        return
    war = await db.get_war(war_id)
    if war and war["ticket_channel_id"]:
        await db.delete_ticket_activity(war["ticket_channel_id"])
    await db.set_war_result(war_id, guild_data["id"])
    await interaction.response.send_message(f"🏆 **{winning_guild}** wins War #{war_id}!")


@bot.tree.command(name="war_leaderboard", description="Top guilds by war wins")
async def war_leaderboard(interaction: discord.Interaction):
    rows = await db.get_war_leaderboard(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("No completed wars yet.", ephemeral=True)
        return
    embed = discord.Embed(title="🔱 War Leaderboard", color=0x1455A4)
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else f"#{i+1}"
        embed.add_field(name=f"{medal} {row['name']}", value=f"{row['wins']}W / {row['losses']}L", inline=False)
    await interaction.response.send_message(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  ON READY
# ═══════════════════════════════════════════════════════════════════════════════

COMMANDS_VERSION = "v6"  # Bump whenever commands change

@bot.event
async def on_ready():
    await db.init_db()

    # Register all persistent views
    bot.add_view(WagerStartView())
    bot.add_view(WagerTicketView())
    bot.add_view(WagerExtensionView())
    bot.add_view(WarStartView())
    bot.add_view(WarTicketView())
    bot.add_view(GuildInviteView())

    # Start the background activity monitor
    if not ticket_activity_task.is_running():
        ticket_activity_task.start()

    # Only sync slash commands when they've actually changed
    import aiosqlite
    async with aiosqlite.connect("deepleague.db") as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT)")
        await conn.commit()
        async with conn.execute("SELECT value FROM bot_meta WHERE key='commands_version'") as cur:
            row = await cur.fetchone()
            stored_version = row[0] if row else None

    if stored_version != COMMANDS_VERSION:
        print(f"⚙️  Commands changed ({stored_version} → {COMMANDS_VERSION}), syncing...")
        await bot.tree.sync()
        async with aiosqlite.connect("deepleague.db") as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO bot_meta (key, value) VALUES ('commands_version', ?)",
                (COMMANDS_VERSION,)
            )
            await conn.commit()
        print("✅  Commands synced.")
    else:
        print("✅  Commands up to date — skipping sync.")

    print(f"✅  Deepleague bot online as {bot.user}")


bot.run(TOKEN)
