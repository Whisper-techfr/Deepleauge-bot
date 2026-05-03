import aiosqlite

DB = "deepleague.db"

async def init_db():
    async with aiosqlite.connect(DB) as db:
        # Wagers
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wagers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                status TEXT DEFAULT 'pending',
                winner TEXT,
                message_id INTEGER,
                channel_id INTEGER,
                ticket_channel_id INTEGER,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wager_players (
                wager_id INTEGER,
                user_id INTEGER,
                team TEXT,
                accepted INTEGER DEFAULT 0,
                FOREIGN KEY(wager_id) REFERENCES wagers(id)
            )
        """)

        # Guilds
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guilds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                server_id INTEGER,
                leader_id INTEGER,
                co_leader_id INTEGER,
                description TEXT DEFAULT '',
                forum_message_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_members (
                guild_id INTEGER,
                user_id INTEGER,
                role TEXT,
                accepted INTEGER DEFAULT 0,
                FOREIGN KEY(guild_id) REFERENCES guilds(id)
            )
        """)

        # Wars
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_a_id INTEGER,
                guild_b_id INTEGER,
                region TEXT,
                status TEXT DEFAULT 'pending',
                winner_guild_id INTEGER,
                ticket_channel_id INTEGER,
                message_id INTEGER,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(guild_a_id) REFERENCES guilds(id),
                FOREIGN KEY(guild_b_id) REFERENCES guilds(id)
            )
        """)

        # Guild cooldowns (merc cooldown)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_cooldowns (
                user_id INTEGER PRIMARY KEY,
                left_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()


# ── WAGER FUNCTIONS ──────────────────────────────────────────────────────────

async def create_wager(type_, created_by):
    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            "INSERT INTO wagers (type, created_by) VALUES (?,?)",
            (type_, created_by)
        )
        await db.commit()
        return cursor.lastrowid


async def add_wager_player(wager_id, user_id, team, accepted=0):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO wager_players VALUES (?,?,?,?)",
            (wager_id, user_id, team, accepted)
        )
        await db.commit()


async def get_wager(wager_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM wagers WHERE id=?", (wager_id,)) as cursor:
            return await cursor.fetchone()


async def get_wager_players(wager_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM wager_players WHERE wager_id=?", (wager_id,)) as cursor:
            return await cursor.fetchall()


async def accept_wager_player(wager_id, user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE wager_players SET accepted=1 WHERE wager_id=? AND user_id=?",
            (wager_id, user_id)
        )
        async with db.execute(
            "SELECT COUNT(*) FROM wager_players WHERE wager_id=? AND accepted=0", (wager_id,)
        ) as cursor:
            row = await cursor.fetchone()
            all_accepted = row[0] == 0
        if all_accepted:
            await db.execute("UPDATE wagers SET status='open' WHERE id=?", (wager_id,))
        await db.commit()
        return all_accepted


async def set_wager_ticket_channel(wager_id, channel_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE wagers SET ticket_channel_id=? WHERE id=?", (channel_id, wager_id))
        await db.commit()


async def set_wager_result(wager_id, winner):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE wagers SET status='done', winner=? WHERE id=?", (winner, wager_id))
        await db.commit()


async def cancel_wager(wager_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE wagers SET status='cancelled' WHERE id=?", (wager_id,))
        await db.commit()


async def get_wager_leaderboard(guild_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT wp.user_id,
                   COUNT(CASE WHEN w.winner = wp.team THEN 1 END) as wins,
                   COUNT(CASE WHEN w.winner != wp.team AND w.status='done' THEN 1 END) as losses
            FROM wager_players wp
            JOIN wagers w ON w.id = wp.wager_id
            WHERE w.status = 'done'
            GROUP BY wp.user_id
            ORDER BY wins DESC
            LIMIT 10
        """) as cursor:
            return await cursor.fetchall()


# ── GUILD FUNCTIONS ──────────────────────────────────────────────────────────

async def register_guild(name, server_id, leader_id):
    async with aiosqlite.connect(DB) as db:
        try:
            cursor = await db.execute(
                "INSERT INTO guilds (name, server_id, leader_id) VALUES (?,?,?)",
                (name, server_id, leader_id)
            )
            guild_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO guild_members VALUES (?,?,?,?)",
                (guild_id, leader_id, "leader", 1)
            )
            await db.commit()
            return guild_id
        except aiosqlite.IntegrityError:
            return None


async def get_guild_by_name(name, server_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM guilds WHERE LOWER(name)=LOWER(?) AND server_id=?", (name, server_id)
        ) as cursor:
            return await cursor.fetchone()


async def get_guild_by_leader(user_id, server_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM guilds WHERE leader_id=? AND server_id=?", (user_id, server_id)
        ) as cursor:
            return await cursor.fetchone()


async def get_guild_by_member(user_id, server_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT g.* FROM guilds g
            JOIN guild_members gm ON g.id = gm.guild_id
            WHERE gm.user_id=? AND g.server_id=?
        """, (user_id, server_id)) as cursor:
            return await cursor.fetchone()


async def get_guild_members(guild_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM guild_members WHERE guild_id=?", (guild_id,)
        ) as cursor:
            return await cursor.fetchall()


async def add_guild_member(guild_id, user_id, role):
    async with aiosqlite.connect(DB) as db:
        role_limits = {
            "co_leader": 1, "manager": 2,
            "main_roster": 5, "sub_roster": 5
        }
        if role in role_limits:
            async with db.execute(
                "SELECT COUNT(*) FROM guild_members WHERE guild_id=? AND role=?",
                (guild_id, role)
            ) as cursor:
                row = await cursor.fetchone()
                if row[0] >= role_limits[role]:
                    return False, f"Max {role_limits[role]} {role} reached"
        await db.execute(
            "INSERT INTO guild_members VALUES (?,?,?,?)", (guild_id, user_id, role, 0)
        )
        await db.commit()
        return True, "ok"


async def accept_guild_member(guild_id, user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE guild_members SET accepted=1 WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )
        await db.commit()


async def get_guild(guild_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM guilds WHERE id=?", (guild_id,)) as cursor:
            return await cursor.fetchone()


async def set_guild_co_leader(guild_id, user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE guilds SET co_leader_id=? WHERE id=?", (user_id, guild_id))
        await db.commit()


async def disband_guild(guild_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM guild_members WHERE guild_id=?", (guild_id,))
        await db.execute("DELETE FROM guilds WHERE id=?", (guild_id,))
        await db.commit()


async def remove_guild_member(guild_id, user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "DELETE FROM guild_members WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )
        await db.commit()


async def set_guild_description(guild_id, description):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE guilds SET description=? WHERE id=?", (description, guild_id))
        await db.commit()


async def set_guild_forum_message(guild_id, message_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE guilds SET forum_message_id=? WHERE id=?", (message_id, guild_id))
        await db.commit()


async def transfer_guild_leader(guild_id, new_leader_id, old_leader_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE guilds SET leader_id=? WHERE id=?", (new_leader_id, guild_id))
        # Old leader becomes main_roster, new leader role updated
        await db.execute("UPDATE guild_members SET role='main_roster' WHERE guild_id=? AND user_id=?", (guild_id, old_leader_id))
        await db.execute("UPDATE guild_members SET role='leader' WHERE guild_id=? AND user_id=?", (guild_id, new_leader_id))
        await db.commit()


# ── WAR FUNCTIONS ────────────────────────────────────────────────────────────

async def create_war(guild_a_id, guild_b_id, region, created_by):
    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            "INSERT INTO wars (guild_a_id, guild_b_id, region, created_by) VALUES (?,?,?,?)",
            (guild_a_id, guild_b_id, region, created_by)
        )
        await db.commit()
        return cursor.lastrowid


async def get_war(war_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM wars WHERE id=?", (war_id,)) as cursor:
            return await cursor.fetchone()


async def set_war_ticket_channel(war_id, channel_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE wars SET ticket_channel_id=? WHERE id=?", (channel_id, war_id))
        await db.commit()


async def accept_war(war_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE wars SET status='open' WHERE id=?", (war_id,))
        await db.commit()


async def cancel_war(war_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE wars SET status='cancelled' WHERE id=?", (war_id,))
        await db.commit()


async def set_war_result(war_id, winner_guild_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE wars SET status='done', winner_guild_id=? WHERE id=?",
            (winner_guild_id, war_id)
        )
        await db.commit()


async def get_war_leaderboard(server_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT g.name, g.id,
                   COUNT(CASE WHEN w.winner_guild_id = g.id THEN 1 END) as wins,
                   COUNT(CASE WHEN w.winner_guild_id != g.id AND w.status='done' THEN 1 END) as losses
            FROM guilds g
            LEFT JOIN wars w ON (w.guild_a_id = g.id OR w.guild_b_id = g.id)
            WHERE g.server_id=? AND w.status='done'
            GROUP BY g.id
            ORDER BY wins DESC
            LIMIT 10
        """, (server_id,)) as cursor:
            return await cursor.fetchall()


# ── COOLDOWN FUNCTIONS ───────────────────────────────────────────────────────

async def set_guild_cooldown(user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_cooldowns (user_id, left_at) VALUES (?, CURRENT_TIMESTAMP)",
            (user_id,)
        )
        await db.commit()


async def check_guild_cooldown(user_id):
    """Returns (on_cooldown, hours_remaining)"""
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT left_at FROM guild_cooldowns WHERE user_id=?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, 0
            from datetime import datetime, timezone
            left_at = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            diff = now - left_at
            hours_passed = diff.total_seconds() / 3600
            if hours_passed < 48:
                hours_remaining = 48 - hours_passed
                return True, hours_remaining
            return False, 0


async def clear_guild_cooldown(user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM guild_cooldowns WHERE user_id=?", (user_id,))
        await db.commit()
