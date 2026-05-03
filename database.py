import aiosqlite

DB = "wagers.db"

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wagers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                type TEXT,
                location TEXT,
                condition TEXT,
                restrictions TEXT DEFAULT 'No restrictions',
                echo_bet INTEGER,
                status TEXT DEFAULT 'pending',
                winner TEXT,
                message_id INTEGER,
                channel_id INTEGER,
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
        await db.commit()


async def create_wager(title, type_, location, condition, restrictions, echo_bet, created_by):
    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            "INSERT INTO wagers (title, type, location, condition, restrictions, echo_bet, created_by) VALUES (?,?,?,?,?,?,?)",
            (title, type_, location, condition, restrictions, echo_bet, created_by)
        )
        await db.commit()
        return cursor.lastrowid


async def add_player(wager_id, user_id, team, accepted=0):
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


async def get_user_wagers(user_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT w.* FROM wagers w
            JOIN wager_players wp ON w.id = wp.wager_id
            WHERE wp.user_id=?
            ORDER BY w.created_at DESC LIMIT 10
        """, (user_id,)) as cursor:
            return await cursor.fetchall()


async def get_server_wagers(channel_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM wagers WHERE channel_id=? AND status != 'done'
            ORDER BY created_at DESC LIMIT 10
        """, (channel_id,)) as cursor:
            return await cursor.fetchall()


async def accept_wager(wager_id, user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE wager_players SET accepted=1 WHERE wager_id=? AND user_id=?",
            (wager_id, user_id)
        )
        # Check if all players accepted
        async with db.execute(
            "SELECT COUNT(*) FROM wager_players WHERE wager_id=? AND accepted=0",
            (wager_id,)
        ) as cursor:
            row = await cursor.fetchone()
            all_accepted = row[0] == 0
        if all_accepted:
            await db.execute("UPDATE wagers SET status='open' WHERE id=?", (wager_id,))
        await db.commit()
        return all_accepted


async def set_result(wager_id, winner):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE wagers SET status='done', winner=? WHERE id=?",
            (winner, wager_id)
        )
        await db.commit()


async def cancel_wager(wager_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE wagers SET status='cancelled' WHERE id=?", (wager_id,))
        await db.commit()


async def save_message(wager_id, message_id, channel_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE wagers SET message_id=?, channel_id=? WHERE id=?",
            (message_id, channel_id, wager_id)
        )
        await db.commit()


async def get_leaderboard():
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT wp.user_id,
                   SUM(CASE WHEN w.winner = wp.team THEN w.echo_bet ELSE -w.echo_bet END) as net_echoes,
                   COUNT(CASE WHEN w.winner = wp.team THEN 1 END) as wins,
                   COUNT(CASE WHEN w.winner != wp.team AND w.status='done' THEN 1 END) as losses
            FROM wager_players wp
            JOIN wagers w ON w.id = wp.wager_id
            WHERE w.status = 'done'
            GROUP BY wp.user_id
            ORDER BY net_echoes DESC
            LIMIT 10
        """) as cursor:
            return await cursor.fetchall()
