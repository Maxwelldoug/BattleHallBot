import unittest
import sqlite3
import os
import math

import db
from fp.helpers import normalize_name

class TestBattleHallDB(unittest.TestCase):
    def setUp(self):
        db.DB_FILE = "test_battlehall.db"
        if os.path.exists(db.DB_FILE):
            os.remove(db.DB_FILE)
        db.init_db()

    def tearDown(self):
        if os.path.exists(db.DB_FILE):
            os.remove(db.DB_FILE)

    def test_init_and_types(self):
        conn = sqlite3.connect(db.DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM types;")
        self.assertEqual(cursor.fetchone()[0], 18)
        
        # Test get_type_by_canonical_name
        row = db.get_type_by_canonical_name("fire")
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "Fire")
        
        row_none = db.get_type_by_canonical_name("nonexistent")
        self.assertIsNone(row_none)
        conn.close()

    def test_player_and_wins(self):
        player_id = db.get_or_create_player("maxwelldoug", "Maxwelldoug")
        self.assertIsNotNone(player_id)
        
        # Test case-insensitive/display name updates
        player_id_2 = db.get_or_create_player("maxwelldoug", "MaxwellDoug")
        self.assertEqual(player_id, player_id_2)
        
        # Check wins initially
        type_row = db.get_type_by_canonical_name("fire")
        type_id = type_row[0]
        wins = db.get_player_wins(player_id, type_id)
        self.assertEqual(wins, 0)
        
        # Increment wins
        db.increment_player_wins(player_id, type_id)
        wins = db.get_player_wins(player_id, type_id)
        self.assertEqual(wins, 1)
        
        # Check other types with wins count
        other_type_row = db.get_type_by_canonical_name("water")
        other_type_id = other_type_row[0]
        count = db.get_other_types_with_wins_count(player_id, other_type_id)
        self.assertEqual(count, 1) # "fire" has 1 win
        
        # Max win cap (10)
        for _ in range(15):
            db.increment_player_wins(player_id, type_id)
        wins = db.get_player_wins(player_id, type_id)
        self.assertEqual(wins, 10)
        
        # Reset all wins
        db.reset_player_all_wins(player_id)
        wins = db.get_player_wins(player_id, type_id)
        self.assertEqual(wins, 0)

class TestBattleHallFormula(unittest.TestCase):
    def calculate_challenge_level(self, player_level: int, current_type_wins: int, other_types_with_wins: int) -> int:
        level_player = float(player_level)
        sqrt_lp = math.sqrt(level_player)
        level_base = level_player - (3.0 * sqrt_lp)
        increment = sqrt_lp / 5.0
        rank = current_type_wins + 1
        val = level_base + (other_types_with_wins / 2.0) + ((rank - 1) * increment)
        calculated = math.ceil(val)
        challenge_level = min(int(level_player), int(calculated))
        return max(1, min(100, challenge_level))

    def test_formula_cases(self):
        # Level 50 player, 0 wins, 0 other types
        # Levelplayer = 50, Levelbase = 50 - 3*sqrt(50) = 50 - 21.21 = 28.79
        # increment = sqrt(50)/5 = 7.07/5 = 1.414
        # rank = 1 -> rank-1 = 0
        # val = 28.79 + 0 + 0 = 28.79 -> ceil -> 29
        # min(50, 29) = 29
        self.assertEqual(self.calculate_challenge_level(50, 0, 0), 29)
        
        # Level 50 player, rank 10 (9 wins), 5 other types
        # val = 28.79 + 5/2 + 9 * 1.414 = 28.79 + 2.5 + 12.726 = 44.016 -> ceil -> 45
        # min(50, 45) = 45
        self.assertEqual(self.calculate_challenge_level(50, 9, 5), 45)
        
        # Level 100 player, rank 10 (9 wins), 17 other types
        # Levelbase = 100 - 3*10 = 70
        # increment = 10/5 = 2.0
        # val = 70 + 17/2 + 9 * 2.0 = 70 + 8.5 + 18.0 = 96.5 -> ceil -> 97
        # min(100, 97) = 97
        self.assertEqual(self.calculate_challenge_level(100, 9, 17), 97)
