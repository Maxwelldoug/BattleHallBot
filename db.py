import sqlite3
import os
import logging

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
    conn = sqlite3.connect(DB_FILE, timeout=5.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def init_db():
    conn = get_connection()
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
            player_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL,
            type_id INTEGER,
            player_level INTEGER,
            challenge_level INTEGER,
            room_context TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
            FOREIGN KEY (type_id) REFERENCES types(id) ON DELETE SET NULL
        );
    """)

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
    
    # Populate types if empty
    cursor.execute("SELECT COUNT(*) FROM types;")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("""
            INSERT INTO types (printed_name, canonical_name)
            VALUES (?, ?);
        """, TYPES)
        conn.commit()
        logger.info("Initialized 18 Pokemon types in the database.")
        
    conn.close()

def get_or_create_player(userid, display_name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, display_name FROM players WHERE userid = ?;", (userid,))
    row = cursor.fetchone()
    if row:
        player_id, old_display = row
        if old_display != display_name:
            cursor.execute("UPDATE players SET display_name = ? WHERE id = ?;", (display_name, player_id))
            conn.commit()
    else:
        cursor.execute("INSERT INTO players (userid, display_name) VALUES (?, ?);", (userid, display_name))
        conn.commit()
        player_id = cursor.lastrowid
    conn.close()
    return player_id

def get_player_by_userid(userid):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, userid, display_name FROM players WHERE userid = ?;", (userid,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "userid": row[1], "display_name": row[2]}
    return None

def get_type_by_canonical_name(canonical_name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, printed_name FROM types WHERE canonical_name = ?;", (canonical_name,))
    row = cursor.fetchone()
    conn.close()
    return row  # returns (id, printed_name) or None

def get_type_by_id(type_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, printed_name, canonical_name FROM types WHERE id = ?;", (type_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_player_wins(player_id, type_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT wins FROM battles WHERE player_id = ? AND type_id = ?;", (player_id, type_id))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def get_other_types_with_wins_count(player_id, exclude_type_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM battles 
        WHERE player_id = ? AND type_id != ? AND wins > 0;
    """, (player_id, exclude_type_id))
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_player_total_wins(player_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(wins) FROM battles WHERE player_id = ?;", (player_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else 0

def increment_player_wins(player_id, type_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO battles (player_id, type_id, wins)
        VALUES (?, ?, 1)
        ON CONFLICT(player_id, type_id) DO UPDATE SET wins = MIN(wins + 1, 10);
    """, (player_id, type_id))
    conn.commit()
    conn.close()

def reset_player_all_wins(player_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE battles SET wins = 0 WHERE player_id = ?;", (player_id,))
    conn.commit()
    conn.close()

def save_session(player_id, mode, type_id, player_level, challenge_level, room_context, status="active"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sessions (player_id, mode, type_id, player_level, challenge_level, room_context, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(player_id) DO UPDATE SET
            mode = excluded.mode,
            type_id = excluded.type_id,
            player_level = excluded.player_level,
            challenge_level = excluded.challenge_level,
            room_context = excluded.room_context,
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP;
    """, (player_id, mode, type_id, player_level, challenge_level, room_context, status))
    conn.commit()
    conn.close()

def get_session(player_id, mode="battlehall"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.player_id, s.mode, s.type_id, t.printed_name, t.canonical_name,
               s.player_level, s.challenge_level, s.room_context, s.status, s.updated_at
        FROM sessions s
        LEFT JOIN types t ON s.type_id = t.id
        WHERE s.player_id = ? AND s.mode = ?;
    """, (player_id, mode))
    row = cursor.fetchone()
    conn.close()
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE player_id = ? AND mode = ?;", (player_id, mode))
    conn.commit()
    conn.close()

def record_match(player_id, mode, battle_tag, opponent_details, result, replay_url=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO match_history (player_id, mode, battle_tag, opponent_details, result, replay_url)
        VALUES (?, ?, ?, ?, ?, ?);
    """, (player_id, mode, battle_tag, opponent_details, result, replay_url))
    conn.commit()
    conn.close()

def get_match_history(player_id, limit=10):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, player_id, mode, battle_tag, opponent_details, result, replay_url, finished_at
        FROM match_history
        WHERE player_id = ?
        ORDER BY id DESC LIMIT ?;
    """, (player_id, limit))
    rows = cursor.fetchall()
    conn.close()
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
