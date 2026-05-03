import discord
from discord import app_commands
from discord.ext import commands
import database as db

# ─── CONFIG ───────────────────────────────────────────────────────────────────
import os
TOKEN = os.getenv("DISCORD_TOKEN")
WAGER_LOBBY_CHANNEL_ID = 0   # Put the channel ID where /wager_start embed lives
GUILD_LEADER_ROLE_NAME = "Guild Leader"  # Role name given to guild leaders
GUILD_CHANNEL_NAME = "guilds"           # Channel name where guild embeds are posted
# ──────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


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


class Wager1v1Modal(discord.ui.Modal, title="Create 1v1 Wager"):
    opponent = discord.ui.TextInput(
        label="Opponent",
        placeholder="Enter their @mention or user ID",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # Parse opponent
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
    teammate = discord.ui.TextInput(
        label="Your Teammate",
        placeholder="Enter their @mention or user ID",
        required=True
    )
    opponent1 = discord.ui.TextInput(
        label="Opponent 1",
        placeholder="Enter their @mention or user ID",
        required=True
    )
    opponent2 = discord.ui.TextInput(
        label="Opponent 2",
        placeholder="Enter their @mention or user ID",
        required=True
    )

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
        team_a = [interaction.user, teammate]
        team_b = [opp1, opp2]
        await create_wager_ticket(interaction, "2v2", team_a, team_b)


async def create_wager_ticket(interaction: discord.Interaction, type_: str, team_a: list, team_b: list):
    guild = interaction.guild

    # Save to DB
    wager_id = await db.create_wager(type_, interaction.user.id)
    for m in team_a:
        accepted = 1 if m.id == interaction.user.id else 0
        await db.add_wager_player(wager_id, m.id, "A", accepted)
    for m in team_b:
        await db.add_wager_player(wager_id, m.id, "B", 0)

    all_players = team_a + team_b

    # Create private ticket channel
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

    # Build embed
    embed = build_wager_embed(wager_id, type_, team_a, team_b, "PENDING")
    view = WagerTicketView(wager_id, [m.id for m in team_b])

    msg = await ticket_channel.send(
        content=" ".join(m.mention for m in all_players),
        embed=embed,
        view=view
    )

    await interaction.followup.send(f"✅ Ticket created! {ticket_channel.mention}", ephemeral=True)


def build_wager_embed(wager_id, type_, team_a, team_b, status):
    color = 0xF59E0B if status == "PENDING" else 0x22C55E if status == "LIVE" else 0x6B7280

    embed = discord.Embed(
        title=f"⚔️ Wager #{wager_id} — {type_}",
        color=color
    )

    def fmt(members):
        return "\n".join(f"<@{m.id if hasattr(m, 'id') else m}>" for m in members) or "—"

    embed.add_field(name="🔵 Team A", value=fmt(team_a), inline=True)
    embed.add_field(name="🔴 Team B", value=fmt(team_b), inline=True)
    embed.add_field(name="📊 Status", value=status, inline=False)
    embed.set_footer(text=f"Wager #{wager_id} • Deepleague")
    return embed


class WagerTicketView(discord.ui.View):
    def __init__(self, wager_id: int, team_b_ids: list):
        super().__init__(timeout=None)
        self.wager_id = wager_id
        self.team_b_ids = team_b_ids

    @discord.ui.button(label="✅ Accept Ticket", style=discord.ButtonStyle.success, custom_id="waccept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        wager_id = self.wager_id
        players = await db.get_wager_players(wager_id)
        player_ids = [p["user_id"] for p in players]

        if interaction.user.id not in player_ids:
            await interaction.response.send_message("❌ You're not part of this wager.", ephemeral=True)
            return

        all_accepted = await db.accept_wager_player(wager_id, interaction.user.id)

        if all_accepted:
            # Allow everyone to type now
            for m_id in player_ids:
                member = interaction.guild.get_member(m_id)
                if member:
                    await interaction.channel.set_permissions(member, read_messages=True, send_messages=True)

            wager = await db.get_wager(wager_id)
            players = await db.get_wager_players(wager_id)
            team_a = [p["user_id"] for p in players if p["team"] == "A"]
            team_b = [p["user_id"] for p in players if p["team"] == "B"]
            embed = build_wager_embed(wager_id, wager["type"], team_a, team_b, "LIVE")

            self.accept.disabled = True
            self.dodge.disabled = True
            await interaction.message.edit(embed=embed, view=self)
            await interaction.response.send_message("✅ All players accepted — wager is **LIVE**! Discuss here. 🔱")
        else:
            await interaction.response.send_message(f"✅ {interaction.user.mention} accepted. Waiting for others.", ephemeral=False)

    @discord.ui.button(label="❌ Dodge", style=discord.ButtonStyle.danger, custom_id="wdodge")
    async def dodge(self, interaction: discord.Interaction, button: discord.ui.Button):
        wager_id = self.wager_id
        players = await db.get_wager_players(wager_id)
        player_ids = [p["user_id"] for p in players]

        if interaction.user.id not in player_ids:
            await interaction.response.send_message("❌ You're not part of this wager.", ephemeral=True)
            return

        await db.cancel_wager(wager_id)
        await interaction.response.send_message(f"❌ {interaction.user.mention} dodged the wager. Ticket closing in 5 seconds...")
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete()


@bot.tree.command(name="wager_start", description="Post the wager start panel (Admins only)")
async def wager_start(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🌊 Deepleague Wager System",
        description="Ready to wager? Click a button below to create a ticket!\n\n"
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
        embed.add_field(
            name=f"{medal} {name}",
            value=f"{row['wins']}W / {row['losses']}L",
            inline=False
        )
    await interaction.response.send_message(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  GUILD SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

# ── GUILD HELPERS ────────────────────────────────────────────────────────────

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
    def __init__(self, guild_id: int, user_id: int, guild_name: str, discord_guild_id: int):
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

        # Check cooldown (skip for head mod+)
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

        # Check if already in a guild
        existing = await db.get_guild_by_member(self.user_id, self.discord_guild_id)
        if existing:
            await interaction.response.send_message(
                f"❌ You are already in **{existing['name']}**. Leave first.", ephemeral=True
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


@bot.tree.command(name="register_guild", description="Register your guild")
@app_commands.describe(
    name="Your guild name",
    co_leader="Your co-leader",
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

    # Check if user is already in a guild
    existing = await db.get_guild_by_member(interaction.user.id, interaction.guild_id)
    if existing:
        await interaction.followup.send(f"❌ You are already in a guild (**{existing['name']}**). Leave or disband it first.", ephemeral=True)
        return

    guild_id = await db.register_guild(name, interaction.guild_id, interaction.user.id)
    if guild_id is None:
        await interaction.followup.send("❌ A guild with that name already exists.", ephemeral=True)
        return

    # Add members (accepted=0, leader auto-accepted in DB)
    await db.add_guild_member(guild_id, co_leader.id, "co_leader")
    await db.set_guild_co_leader(guild_id, co_leader.id)
    await db.add_guild_member(guild_id, manager1.id, "manager")
    if manager2:
        await db.add_guild_member(guild_id, manager2.id, "manager")
    for m in [main1, main2, main3, main4, main5]:
        if m:
            await db.add_guild_member(guild_id, m.id, "main_roster")
    for m in [sub1, sub2, sub3, sub4, sub5]:
        if m:
            await db.add_guild_member(guild_id, m.id, "sub_roster")

    # Give Guild Leader role
    role = discord.utils.get(interaction.guild.roles, name=GUILD_LEADER_ROLE_NAME)
    if role:
        try:
            await interaction.user.add_roles(role)
        except Exception:
            pass

    # Post guild embed in #guilds straight away (⏳ for pending members)
    members = await db.get_guild_members(guild_id)
    guild_data = await db.get_guild(guild_id)
    embed = await build_guild_embed(dict(guild_data), [dict(m) for m in members])
    guild_channel = discord.utils.get(interaction.guild.text_channels, name=GUILD_CHANNEL_NAME)
    if guild_channel:
        msg = await guild_channel.send(embed=embed)
        await db.set_guild_forum_message(guild_id, msg.id)
    else:
        await interaction.channel.send(embed=embed)

    await interaction.followup.send("✅ Guild registered! Invites sent to all members.", ephemeral=True)

    # DM all invited members in background so it doesn't block
    import asyncio

    async def send_invites():
        role_map = {
            co_leader: "Co-Leader",
            manager1: "Manager",
            manager2: "Manager",
            main1: "Main Roster", main2: "Main Roster", main3: "Main Roster",
            main4: "Main Roster", main5: "Main Roster",
            sub1: "Sub Roster", sub2: "Sub Roster", sub3: "Sub Roster",
            sub4: "Sub Roster", sub5: "Sub Roster",
        }
        for member, role_label in role_map.items():
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


@bot.tree.command(name="disband_guild", description="Disband your guild (Guild Leader only)")
async def disband_guild(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    my_guild = await db.get_guild_by_leader(interaction.user.id, interaction.guild_id)
    if not my_guild:
        await interaction.followup.send("❌ You are not a Guild Leader.", ephemeral=True)
        return

    guild_name = my_guild["name"]
    members = await db.get_guild_members(my_guild["id"])

    # Delete guild from DB
    await db.disband_guild(my_guild["id"])

    # Remove Guild Leader role
    role = discord.utils.get(interaction.guild.roles, name=GUILD_LEADER_ROLE_NAME)
    if role:
        try:
            await interaction.user.remove_roles(role)
        except Exception:
            pass

    # DM all members and set cooldown
    for m in members:
        if m["user_id"] == interaction.user.id:
            continue
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
    member_id = discord.ui.TextInput(
        label="Member to add",
        placeholder="Enter their @mention or user ID",
        required=True
    )
    role = discord.ui.TextInput(
        label="Role",
        placeholder="main_roster or sub_roster",
        required=True
    )

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

        # Leaders and co-leaders can also add managers
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
            await target.send(
                f"🔱 **You have been added to {guild_data['name']} as {role_input.replace('_', ' ').title()}!**"
            )
        except discord.Forbidden:
            pass

        await interaction.followup.send(f"✅ {target.mention} added as **{role_input.replace('_', ' ').title()}**.")


class TransferLeaderModal(discord.ui.Modal, title="Transfer Guild Leadership"):
    member_id = discord.ui.TextInput(
        label="New Leader",
        placeholder="Enter their @mention or user ID",
        required=True
    )

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

        guild_data = await db.get_guild(self.guild_id)
        await db.transfer_guild_leader(self.guild_id, target.id, interaction.user.id)
        guild_data = await db.get_guild(self.guild_id)

        # Remove Guild Leader role from old leader, give to new
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
    member_id = discord.ui.TextInput(
        label="Member to kick",
        placeholder="Enter their @mention or user ID",
        required=True
    )

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
        target_role = next((m["role"] for m in guild_members if m["user_id"] == target.id), None)

        if not target_role:
            await interaction.followup.send("❌ That person is not in your guild.", ephemeral=True)
            return

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
        await db.set_guild_cooldown(target.id)

        # Update forum embed
        await update_guild_forum(bot, interaction.guild, guild_data)

        try:
            await target.send(f"❌ **You have been kicked from {guild_data['name']}** by <@{interaction.user.id}>. You have a 48-hour merc cooldown before joining another guild.")
        except discord.Forbidden:
            pass

        await interaction.followup.send(f"✅ {target.mention} has been kicked from **{guild_data['name']}**. They now have a 48-hour merc cooldown.")


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
        guild_data = await db.get_guild(self.guild_id)
        await db.set_guild_description(self.guild_id, self.description.value)
        guild_data = await db.get_guild(self.guild_id)

        # Update forum embed
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

    # Check name not taken
    existing = await db.get_guild_by_name(new_name, interaction.guild_id)
    if existing:
        await interaction.followup.send(f"❌ A guild named **{new_name}** already exists.", ephemeral=True)
        return

    async with __import__('aiosqlite').connect('deepleague.db') as db_conn:
        await db_conn.execute("UPDATE guilds SET name=? WHERE id=?", (new_name, my_guild["id"]))
        await db_conn.commit()

    guild_data = await db.get_guild(my_guild["id"])
    await update_guild_forum(bot, interaction.guild, guild_data)

    await interaction.followup.send(f"✅ Guild renamed to **{new_name}**!", ephemeral=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  WAR SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

class WarStartView(discord.ui.View):
    """Persistent view posted by /war_start — lives in lobby channel forever."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚔️ Declare War", style=discord.ButtonStyle.blurple, custom_id="war_declare")
    async def declare_war_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check user is a guild leader
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
        await interaction.followup.send(
            f"⚔️ Challenging **{target['name']}**! Pick the region:",
            view=view,
            ephemeral=True
        )


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

    # Only management (leader, co_leader, manager) in ticket
    mgmt_roles = {"leader", "co_leader", "manager"}
    mgmt_a = [m["user_id"] for m in members_a if m["role"] in mgmt_roles]
    mgmt_b = [m["user_id"] for m in members_b if m["role"] in mgmt_roles]
    # Also include guild leaders
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

    embed = discord.Embed(
        title=f"⚔️ War #{war_id} — {guild_a['name']} vs {guild_b['name']}",
        color=0xF59E0B
    )
    embed.add_field(name="🔵 Guild A", value=guild_a["name"], inline=True)
    embed.add_field(name="🔴 Guild B", value=guild_b["name"], inline=True)
    embed.add_field(name="🌍 Region", value=region, inline=True)
    embed.add_field(name="📊 Status", value="PENDING", inline=False)
    embed.set_footer(text=f"War #{war_id} • Only Guild Leader or Co-Leader can accept/dodge")

    view = WarTicketView(war_id, guild_b_id, mgmt_b)

    mentions = " ".join(f"<@{uid}>" for uid in all_mgmt)
    await ticket_channel.send(content=mentions, embed=embed, view=view)
    await interaction.followup.send(f"✅ War ticket created! {ticket_channel.mention}", ephemeral=True)


class WarTicketView(discord.ui.View):
    def __init__(self, war_id: int, guild_b_id: int, mgmt_b_ids: list):
        super().__init__(timeout=None)
        self.war_id = war_id
        self.guild_b_id = guild_b_id
        self.mgmt_b_ids = mgmt_b_ids

    @discord.ui.button(label="✅ Accept War", style=discord.ButtonStyle.success, custom_id="waraccept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only leader or co_leader of guild B can accept
        guild_b = await db.get_guild(self.guild_b_id)
        allowed = [guild_b["leader_id"], guild_b["co_leader_id"]]
        if interaction.user.id not in allowed:
            await interaction.response.send_message("❌ Only the Guild Leader or Co-Leader of the challenged guild can accept.", ephemeral=True)
            return

        await db.accept_war(self.war_id)

        # Allow all mgmt to type
        for uid in self.mgmt_b_ids:
            member = interaction.guild.get_member(uid)
            if member:
                await interaction.channel.set_permissions(member, read_messages=True, send_messages=True)

        war = await db.get_war(self.war_id)
        guild_a = await db.get_guild(war["guild_a_id"])
        guild_b_data = await db.get_guild(war["guild_b_id"])

        embed = discord.Embed(
            title=f"⚔️ War #{self.war_id} — {guild_a['name']} vs {guild_b_data['name']}",
            color=0x22C55E
        )
        embed.add_field(name="🔵 Guild A", value=guild_a["name"], inline=True)
        embed.add_field(name="🔴 Guild B", value=guild_b_data["name"], inline=True)
        embed.add_field(name="🌍 Region", value=war["region"], inline=True)
        embed.add_field(name="📊 Status", value="LIVE 🔥", inline=False)

        self.accept.disabled = True
        self.dodge.disabled = True
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message("✅ War accepted — **IT'S ON!** 🔱")

    @discord.ui.button(label="❌ Dodge", style=discord.ButtonStyle.danger, custom_id="wardodge")
    async def dodge(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_b = await db.get_guild(self.guild_b_id)
        allowed = [guild_b["leader_id"], guild_b["co_leader_id"]]
        if interaction.user.id not in allowed:
            await interaction.response.send_message("❌ Only the Guild Leader or Co-Leader can dodge.", ephemeral=True)
            return

        await db.cancel_war(self.war_id)
        await interaction.response.send_message(f"❌ {interaction.user.mention} dodged the war. Ticket closing in 5 seconds...")
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete()


@bot.tree.command(name="declare_war", description="Challenge another guild to a war (Guild Leaders only)")
@app_commands.describe(target_guild="Name of the guild you want to war")
async def declare_war(interaction: discord.Interaction, target_guild: str):
    await interaction.response.defer(ephemeral=True)

    # Check challenger is a guild leader
    my_guild = await db.get_guild_by_leader(interaction.user.id, interaction.guild_id)
    if not my_guild:
        await interaction.followup.send("❌ You must be a Guild Leader to declare war.", ephemeral=True)
        return

    # Find target guild
    target = await db.get_guild_by_name(target_guild, interaction.guild_id)
    if not target:
        await interaction.followup.send(f"❌ Guild '{target_guild}' not found.", ephemeral=True)
        return

    if target["id"] == my_guild["id"]:
        await interaction.followup.send("❌ You can't war your own guild.", ephemeral=True)
        return

    # Ask for region
    view = WarRegionView(my_guild["id"], target["id"])
    await interaction.followup.send(
        f"⚔️ Challenging **{target['name']}** to a war! Pick the region:",
        view=view,
        ephemeral=True
    )


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
        embed.add_field(
            name=f"{medal} {row['name']}",
            value=f"{row['wins']}W / {row['losses']}L",
            inline=False
        )
    await interaction.response.send_message(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  ON READY
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await db.init_db()
    bot.add_view(WagerStartView())
    bot.add_view(WarStartView())
    await bot.tree.sync()
    print(f"✅  Deepleague bot online as {bot.user}")


bot.run(TOKEN)
