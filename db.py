import sqlite3
import os
import logging
from contextlib import contextmanager
from typing import Dict, Optional, List, Any

logger = logging.getLogger(__name__)

DB_FILE = "battlehall.db"

TYPES = [
    ("Normal", "normal"),
    ("Fire", "fire"),
    ("Water", "water"),
    ("Grass", "grass"),
    ("Electric", "electric"),
    ("Ice", "ice"),
    ("Fighting", "fighting"),
    ("Poison", "poison"),
    ("Ground", "ground"),
    ("Flying", "flying"),
    ("Psychic", "psychic"),
    ("Bug", "bug"),
    ("Rock", "rock"),
    ("Ghost", "ghost"),
    ("Dragon", "dragon"),
    ("Steel", "steel"),
    ("Dark", "dark"),
    ("Fairy", "fairy"),
]

def get_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn

@contextmanager
def get_db():
    conn = get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def init_db():
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Enable WAL mode and normal synchronous for concurrent throughput
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")

        # Create types table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printed_name TEXT NOT NULL,
                canonical_name TEXT NOT NULL UNIQUE
            );
        """)

        # Populate types if empty
        cursor.execute("SELECT COUNT(*) FROM types;")
        if cursor.fetchone()[0] == 0:
            cursor.executemany("""
                INSERT INTO types (printed_name, canonical_name)
                VALUES (?, ?);
            """, TYPES)
            conn.commit()
            logger.info("Initialized 18 Pokemon types in the database.")

        # Create players table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL
            );
        """)

        # Create battles table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS battles (
                player_id INTEGER NOT NULL,
                type_id INTEGER NOT NULL,
                wins INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (player_id, type_id),
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
                FOREIGN KEY (type_id) REFERENCES types(id) ON DELETE CASCADE
            );
        """)

        # Create sessions table for persistence across bot restarts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                player_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                type_id INTEGER,
                player_level INTEGER,
                challenge_level INTEGER,
                room_context TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (player_id, mode),
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
                FOREIGN KEY (type_id) REFERENCES types(id) ON DELETE SET NULL
            );
        """)

        # Check if existing sessions table needs migration to composite primary key (player_id, mode)
        cursor.execute("PRAGMA table_info(sessions);")
        session_cols = cursor.fetchall()
        if session_cols:
            pk_cols = [col[1] for col in session_cols if col[5] > 0]
            if pk_cols != ["player_id", "mode"]:
                cursor.execute("PRAGMA foreign_keys = OFF;")
                cursor.execute("""
                    CREATE TABLE sessions_new (
                        player_id INTEGER NOT NULL,
                        mode TEXT NOT NULL,
                        type_id INTEGER,
                        player_level INTEGER,
                        challenge_level INTEGER,
                        room_context TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (player_id, mode),
                        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
                        FOREIGN KEY (type_id) REFERENCES types(id) ON DELETE SET NULL
                    );
                """)
                cursor.execute("""
                    INSERT OR IGNORE INTO sessions_new (
                        player_id, mode, type_id, player_level, challenge_level, room_context, status, updated_at
                    )
                    SELECT player_id, mode, type_id, player_level, challenge_level, room_context, status, updated_at
                    FROM sessions;
                """)
                cursor.execute("DROP TABLE sessions;")
                cursor.execute("ALTER TABLE sessions_new RENAME TO sessions;")
                conn.commit()
                cursor.execute("PRAGMA foreign_keys = ON;")

        # Create match history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS match_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                battle_tag TEXT,
                opponent_details TEXT,
                result TEXT NOT NULL,
                replay_url TEXT,
                finished_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
            );
        """)

        # Create battle_tower_records table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS battle_tower_records (
                player_id INTEGER NOT NULL,
                format_type TEXT NOT NULL,
                current_streak INTEGER NOT NULL DEFAULT 0,
                max_streak INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (player_id, format_type),
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
            );
        """)

        # Create factory_records table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS factory_records (
                player_id INTEGER PRIMARY KEY,
                current_streak INTEGER NOT NULL DEFAULT 0,
                max_streak INTEGER NOT NULL DEFAULT 0,
                total_wins INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
            );
        """)
        conn.commit()
    finally:
        conn.close()


def get_or_create_player(userid, display_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO players (userid, display_name)
            VALUES (?, ?)
            ON CONFLICT(userid) DO UPDATE SET display_name = excluded.display_name;
        """, (userid, display_name))
        cursor.execute("SELECT id FROM players WHERE userid = ?;", (userid,))
        row = cursor.fetchone()
        return row[0]

def get_player_by_userid(userid):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, userid, display_name FROM players WHERE userid = ?;", (userid,))
        row = cursor.fetchone()
        if row:
            return {"id": row[0], "userid": row[1], "display_name": row[2]}
        return None

def get_type_by_canonical_name(canonical_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, printed_name FROM types WHERE canonical_name = ?;", (canonical_name,))
        row = cursor.fetchone()
        return row  # returns (id, printed_name) or None

def get_type_by_id(type_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, printed_name, canonical_name FROM types WHERE id = ?;", (type_id,))
        row = cursor.fetchone()
        return row

def get_player_wins(player_id, type_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT wins FROM battles WHERE player_id = ? AND type_id = ?;", (player_id, type_id))
        row = cursor.fetchone()
        return row[0] if row else 0

def get_other_types_with_wins_count(player_id, exclude_type_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM battles 
            WHERE player_id = ? AND type_id != ? AND wins > 0;
        """, (player_id, exclude_type_id))
        count = cursor.fetchone()[0]
        return count

def get_player_total_wins(player_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(wins) FROM battles WHERE player_id = ?;", (player_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

def increment_player_wins(player_id, type_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO battles (player_id, type_id, wins)
            VALUES (?, ?, 1)
            ON CONFLICT(player_id, type_id) DO UPDATE SET wins = MIN(wins + 1, 10);
        """, (player_id, type_id))

def reset_player_all_wins(player_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE battles SET wins = 0 WHERE player_id = ?;", (player_id,))

def save_session(player_id, mode, type_id, player_level, challenge_level, room_context, status="active"):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sessions (player_id, mode, type_id, player_level, challenge_level, room_context, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(player_id, mode) DO UPDATE SET
                type_id = excluded.type_id,
                player_level = excluded.player_level,
                challenge_level = excluded.challenge_level,
                room_context = excluded.room_context,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP;
        """, (player_id, mode, type_id, player_level, challenge_level, room_context, status))

def get_session(player_id, mode="battlehall"):
    with get_db() as conn:
        cursor = conn.cursor()
        if mode is None:
            cursor.execute("""
                SELECT s.player_id, s.mode, s.type_id, t.printed_name, t.canonical_name,
                       s.player_level, s.challenge_level, s.room_context, s.status, s.updated_at
                FROM sessions s
                LEFT JOIN types t ON s.type_id = t.id
                WHERE s.player_id = ?
                ORDER BY s.updated_at DESC LIMIT 1;
            """, (player_id,))
        else:
            cursor.execute("""
                SELECT s.player_id, s.mode, s.type_id, t.printed_name, t.canonical_name,
                       s.player_level, s.challenge_level, s.room_context, s.status, s.updated_at
                FROM sessions s
                LEFT JOIN types t ON s.type_id = t.id
                WHERE s.player_id = ? AND s.mode = ?;
            """, (player_id, mode))
        row = cursor.fetchone()
    if row:
        return {
            "player_id": row[0],
            "mode": row[1],
            "type_id": row[2],
            "printed_type": row[3],
            "canonical_type": row[4],
            "player_level": row[5],
            "challenge_level": row[6],
            "room_context": row[7],
            "status": row[8],
            "updated_at": row[9]
        }
    return None

def clear_session(player_id, mode="battlehall"):
    with get_db() as conn:
        cursor = conn.cursor()
        if mode is None:
            cursor.execute("DELETE FROM sessions WHERE player_id = ?;", (player_id,))
        else:
            cursor.execute("DELETE FROM sessions WHERE player_id = ? AND mode = ?;", (player_id, mode))

def record_match(player_id, mode, battle_tag, opponent_details, result, replay_url=None):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO match_history (player_id, mode, battle_tag, opponent_details, result, replay_url)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (player_id, mode, battle_tag, opponent_details, result, replay_url))

def get_match_history(player_id, limit=10):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, player_id, mode, battle_tag, opponent_details, result, replay_url, finished_at
            FROM match_history
            WHERE player_id = ?
            ORDER BY id DESC LIMIT ?;
        """, (player_id, limit))
        rows = cursor.fetchall()
    return [
        {
            "id": r[0],
            "player_id": r[1],
            "mode": r[2],
            "battle_tag": r[3],
            "opponent_details": r[4],
            "result": r[5],
            "replay_url": r[6],
            "finished_at": r[7]
        }
        for r in rows
    ]


# --- Battle Tower Persistence Helpers ---

def get_tower_record(player_id: int, format_type: str) -> Dict[str, int]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT current_streak, max_streak
            FROM battle_tower_records
            WHERE player_id = ? AND format_type = ?;
        """, (player_id, format_type))
        row = cursor.fetchone()
    if row:
        return {"current_streak": row[0], "max_streak": row[1]}
    return {"current_streak": 0, "max_streak": 0}

def increment_tower_streak(player_id: int, format_type: str) -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO battle_tower_records (player_id, format_type, current_streak, max_streak)
            VALUES (?, ?, 1, 1)
            ON CONFLICT(player_id, format_type) DO UPDATE SET
                current_streak = current_streak + 1,
                max_streak = MAX(max_streak, current_streak + 1);
        """, (player_id, format_type))
        cursor.execute("""
            SELECT current_streak
            FROM battle_tower_records
            WHERE player_id = ? AND format_type = ?;
        """, (player_id, format_type))
        row = cursor.fetchone()
    return row[0] if row else 1

def reset_tower_streak(player_id: int, format_type: str) -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO battle_tower_records (player_id, format_type, current_streak, max_streak)
            VALUES (?, ?, 0, 0)
            ON CONFLICT(player_id, format_type) DO UPDATE SET
                current_streak = 0;
        """, (player_id, format_type))

def reset_tower_all(player_id: int, format_type: Optional[str] = None) -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        if format_type:
            cursor.execute("""
                UPDATE battle_tower_records
                SET current_streak = 0, max_streak = 0
                WHERE player_id = ? AND format_type = ?;
            """, (player_id, format_type))
        else:
            cursor.execute("""
                UPDATE battle_tower_records
                SET current_streak = 0, max_streak = 0
                WHERE player_id = ?;
            """, (player_id,))

def get_tower_all_records(player_id: int) -> Dict[str, Dict[str, int]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT format_type, current_streak, max_streak
            FROM battle_tower_records
            WHERE player_id = ?;
        """, (player_id,))
        rows = cursor.fetchall()
    records: Dict[str, Dict[str, int]] = {
        "singles": {"current_streak": 0, "max_streak": 0},
        "doubles": {"current_streak": 0, "max_streak": 0},
    }
    for fmt, curr, max_s in rows:
        records[fmt] = {"current_streak": curr, "max_streak": max_s}
    return records


# --- Battle Factory Persistence Helpers ---

def get_factory_record(player_id: int) -> Dict[str, int]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT current_streak, max_streak, total_wins
            FROM factory_records
            WHERE player_id = ?;
        """, (player_id,))
        row = cursor.fetchone()
    if row:
        return {"current_streak": row[0], "max_streak": row[1], "total_wins": row[2]}
    return {"current_streak": 0, "max_streak": 0, "total_wins": 0}

def increment_factory_streak(player_id: int) -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO factory_records (player_id, current_streak, max_streak, total_wins)
            VALUES (?, 1, 1, 1)
            ON CONFLICT(player_id) DO UPDATE SET
                current_streak = current_streak + 1,
                max_streak = MAX(max_streak, current_streak + 1),
                total_wins = total_wins + 1;
        """, (player_id,))
        cursor.execute("""
            SELECT current_streak
            FROM factory_records
            WHERE player_id = ?;
        """, (player_id,))
        row = cursor.fetchone()
    return row[0] if row else 1

def reset_factory_streak(player_id: int) -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO factory_records (player_id, current_streak, max_streak, total_wins)
            VALUES (?, 0, 0, 0)
            ON CONFLICT(player_id) DO UPDATE SET
                current_streak = 0;
        """, (player_id,))

def reset_factory_all(player_id: int) -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE factory_records
            SET current_streak = 0, max_streak = 0, total_wins = 0
            WHERE player_id = ?;
        """, (player_id,))


