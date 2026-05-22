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

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Enable foreign keys
    cursor.execute("PRAGMA foreign_keys = ON;")
    
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
    conn = sqlite3.connect(DB_FILE)
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

def get_type_by_canonical_name(canonical_name):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, printed_name FROM types WHERE canonical_name = ?;", (canonical_name,))
    row = cursor.fetchone()
    conn.close()
    return row  # returns (id, printed_name) or None

def get_player_wins(player_id, type_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT wins FROM battles WHERE player_id = ? AND type_id = ?;", (player_id, type_id))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def get_other_types_with_wins_count(player_id, exclude_type_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM battles 
        WHERE player_id = ? AND type_id != ? AND wins > 0;
    """, (player_id, exclude_type_id))
    count = cursor.fetchone()[0]
    conn.close()
    return count

def increment_player_wins(player_id, type_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO battles (player_id, type_id, wins)
        VALUES (?, ?, 1)
        ON CONFLICT(player_id, type_id) DO UPDATE SET wins = MIN(wins + 1, 10);
    """, (player_id, type_id))
    conn.commit()
    conn.close()

def reset_player_all_wins(player_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE battles SET wins = 0 WHERE player_id = ?;", (player_id,))
    conn.commit()
    conn.close()
