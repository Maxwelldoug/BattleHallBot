import unittest
import os
import sqlite3
import concurrent.futures
from typing import Dict, Any

import db
from fp.managers.command_dispatcher import CommandDispatcher


class MockWSClient:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, room, msgs):
        self.sent_messages.append((room, msgs))


class TestMilestoneM2EmpiricalStress(unittest.TestCase):
    """
    Adversarial and stress tests for Milestone M2:
    1. CommandDispatcher typing bug fix verification.
    2. Concurrency stress on save_session, get_session, and clear_session across multiple modes for the same player.
    3. Migration stress on pre-existing database with old sessions schema.
    4. Streak boundary values (0 to 100+, resets, PB retention, concurrent atomic increments).
    """

    def setUp(self):
        self.test_dbs = []

    def tearDown(self):
        for path in self.test_dbs:
            for ext in ["", "-wal", "-shm"]:
                target = path + ext
                if os.path.exists(target):
                    try:
                        os.remove(target)
                    except OSError:
                        pass

    def _create_isolated_db(self, name: str) -> str:
        db_path = f"/tmp/{name}_{os.getpid()}.db"
        for ext in ["", "-wal", "-shm"]:
            target = db_path + ext
            if os.path.exists(target):
                os.remove(target)
        self.test_dbs.append(db_path)
        db.DB_FILE = db_path
        db.init_db()
        return db_path

    # =========================================================================
    # 1. CommandDispatcher Typing & Instantiation
    # =========================================================================
    def test_command_dispatcher_typing_and_instantiation(self):
        """Verify Any is properly imported and CommandDispatcher accepts any battle/challenge dispatcher."""
        ws = MockWSClient()
        dispatcher = CommandDispatcher(ws, battle_manager="mock_bm", challenge_dispatcher="mock_cd")
        self.assertEqual(dispatcher.battle_manager, "mock_bm")
        self.assertEqual(dispatcher.challenge_dispatcher, "mock_cd")
        self.assertEqual(dispatcher.modes_by_prefix, {})
        self.assertEqual(dispatcher.modes_by_id, {})

    # =========================================================================
    # 2. Concurrency Stress: save_session, get_session, clear_session
    # =========================================================================
    def test_concurrent_sessions_same_player_multiple_modes(self):
        """
        Stress-test concurrent save_session and get_session for the same player across
        battlehall, battletower, and battlefactory with multiple threads.
        """
        db_path = self._create_isolated_db("test_concurrency_sessions")
        p1 = db.get_or_create_player("player_concurrency_1", "PlayerConcurrency1")

        modes = ["battlehall", "battletower", "battlefactory"]
        errors = []

        def worker(thread_id: int):
            try:
                for i in range(30):
                    mode = modes[(thread_id + i) % len(modes)]
                    db.save_session(
                        player_id=p1,
                        mode=mode,
                        type_id=1 if mode == "battlehall" else None,
                        player_level=50,
                        challenge_level=i + 1,
                        room_context=f"room_{thread_id}_{i}",
                        status="active"
                    )
                    sess = db.get_session(p1, mode)
                    if sess is None:
                        errors.append(f"Thread {thread_id} iteration {i}: get_session({p1}, {mode}) returned None")
                    elif sess["mode"] != mode:
                        errors.append(f"Thread {thread_id} iteration {i}: expected mode {mode}, got {sess['mode']}")
            except Exception as e:
                errors.append(f"Thread {thread_id} raised exception: {type(e).__name__}: {e}")

        # Run 15 threads concurrently (450 total operations)
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(worker, tid) for tid in range(15)]
            concurrent.futures.wait(futures)

        self.assertEqual(errors, [], f"Concurrency errors encountered: {errors[:5]}")

        # Verify final sessions for all 3 modes exist independently
        for mode in modes:
            sess = db.get_session(p1, mode)
            self.assertIsNotNone(sess, f"Session for {mode} should exist")
            self.assertEqual(sess["mode"], mode)
            self.assertEqual(sess["player_id"], p1)

    def test_concurrent_clear_and_save_isolation(self):
        """
        Verify that clearing one mode does not delete or corrupt another mode's active session
        under concurrent access.
        """
        db_path = self._create_isolated_db("test_clear_isolation")
        p1 = db.get_or_create_player("p_clear_iso", "PClearIso")

        # Seed 3 sessions
        db.save_session(p1, "battlehall", 1, 50, 10, "lobby_hall", "active")
        db.save_session(p1, "battletower", None, 50, 3, "lobby_tower", "active")
        db.save_session(p1, "battlefactory", None, 50, 1, "lobby_factory", "active")

        errors = []

        def worker_hall_clear(iterations: int):
            try:
                for _ in range(iterations):
                    db.clear_session(p1, "battlehall")
                    db.save_session(p1, "battlehall", 1, 50, 5, "lobby_hall", "active")
            except Exception as e:
                errors.append(f"Hall worker failed: {e}")

        def worker_tower_reader(iterations: int):
            try:
                for _ in range(iterations):
                    sess = db.get_session(p1, "battletower")
                    if sess is None or sess["mode"] != "battletower":
                        errors.append(f"Tower session corrupted or missing: {sess}")
            except Exception as e:
                errors.append(f"Tower reader failed: {e}")

        def worker_factory_reader(iterations: int):
            try:
                for _ in range(iterations):
                    sess = db.get_session(p1, "battlefactory")
                    if sess is None or sess["mode"] != "battlefactory":
                        errors.append(f"Factory session corrupted or missing: {sess}")
            except Exception as e:
                errors.append(f"Factory reader failed: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as executor:
            f1 = [executor.submit(worker_hall_clear, 40) for _ in range(3)]
            f2 = [executor.submit(worker_tower_reader, 40) for _ in range(3)]
            f3 = [executor.submit(worker_factory_reader, 40) for _ in range(3)]
            concurrent.futures.wait(f1 + f2 + f3)

        self.assertEqual(errors, [], f"Crosstalk/concurrency errors during clear: {errors[:5]}")

        # Now test mode=None clear_session clears all modes for p1 only
        p2 = db.get_or_create_player("p_clear_other", "PClearOther")
        db.save_session(p2, "battletower", None, 50, 7, "lobby", "active")

        db.clear_session(p1, mode=None)
        self.assertIsNone(db.get_session(p1, "battlehall"))
        self.assertIsNone(db.get_session(p1, "battletower"))
        self.assertIsNone(db.get_session(p1, "battlefactory"))

        # p2 session must still be intact
        s2 = db.get_session(p2, "battletower")
        self.assertIsNotNone(s2)
        self.assertEqual(s2["challenge_level"], 7)

    # =========================================================================
    # 3. Migration Stress: Pre-existing Database with Old Schema
    # =========================================================================
    def test_migration_from_old_sessions_schema(self):
        """
        Build a legacy database with single-column primary key sessions table, populated
        with real players, hall battles, and active sessions, then migrate via init_db().
        """
        db_path = f"/tmp/test_legacy_migration_{os.getpid()}.db"
        for ext in ["", "-wal", "-shm"]:
            if os.path.exists(db_path + ext):
                os.remove(db_path + ext)
        self.test_dbs.append(db_path)

        # 1. Create legacy schema manually
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printed_name TEXT NOT NULL,
                canonical_name TEXT NOT NULL UNIQUE
            );
        """)
        cursor.executemany("INSERT INTO types (printed_name, canonical_name) VALUES (?, ?);", db.TYPES)

        cursor.execute("""
            CREATE TABLE players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL
            );
        """)

        cursor.execute("""
            CREATE TABLE battles (
                player_id INTEGER NOT NULL,
                type_id INTEGER NOT NULL,
                wins INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (player_id, type_id),
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
                FOREIGN KEY (type_id) REFERENCES types(id) ON DELETE CASCADE
            );
        """)

        # OLD sessions schema: PRIMARY KEY (player_id)
        cursor.execute("""
            CREATE TABLE sessions (
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

        cursor.execute("""
            CREATE TABLE match_history (
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

        # Populate legacy records
        cursor.execute("INSERT INTO players (userid, display_name) VALUES ('legacy_user_1', 'LegacyUser1');")
        p1 = cursor.lastrowid
        cursor.execute("INSERT INTO players (userid, display_name) VALUES ('legacy_user_2', 'LegacyUser2');")
        p2 = cursor.lastrowid

        cursor.execute("INSERT INTO battles (player_id, type_id, wins) VALUES (?, 1, 5);", (p1,))
        cursor.execute("INSERT INTO battles (player_id, type_id, wins) VALUES (?, 2, 8);", (p2,))

        cursor.execute("""
            INSERT INTO sessions (player_id, mode, type_id, player_level, challenge_level, room_context, status)
            VALUES (?, 'battlehall', 1, 50, 6, 'lobby_leg1', 'active');
        """, (p1,))
        cursor.execute("""
            INSERT INTO sessions (player_id, mode, type_id, player_level, challenge_level, room_context, status)
            VALUES (?, 'battlehall', 2, 50, 9, 'lobby_leg2', 'active');
        """, (p2,))

        cursor.execute("""
            INSERT INTO match_history (player_id, mode, battle_tag, opponent_details, result)
            VALUES (?, 'battlehall', 'battle-101', 'Pikachu', 'win');
        """, (p1,))

        conn.commit()
        conn.close()

        # 2. Run new init_db() on pre-existing database
        db.DB_FILE = db_path
        db.init_db()

        # 3. Assertions on migrated database
        verify_conn = db.get_connection()
        v_cur = verify_conn.cursor()

        # Check foreign keys integrity
        v_cur.execute("PRAGMA foreign_key_check;")
        fk_violations = v_cur.fetchall()
        self.assertEqual(fk_violations, [], f"Foreign key violations detected after migration: {fk_violations}")

        # Check primary key of sessions table
        v_cur.execute("PRAGMA table_info(sessions);")
        cols = v_cur.fetchall()
        pk_cols = [c[1] for c in cols if c[5] > 0]
        self.assertEqual(pk_cols, ["player_id", "mode"], f"Migrated sessions table PK should be composite (player_id, mode), got: {pk_cols}")

        # Verify old sessions data preserved intact
        v_cur.execute("SELECT player_id, mode, type_id, challenge_level, room_context FROM sessions ORDER BY player_id;")
        migrated_sessions = v_cur.fetchall()
        self.assertEqual(len(migrated_sessions), 2)
        self.assertEqual(migrated_sessions[0], (p1, "battlehall", 1, 6, "lobby_leg1"))
        self.assertEqual(migrated_sessions[1], (p2, "battlehall", 2, 9, "lobby_leg2"))

        # Verify helper functions work on migrated database
        s1 = db.get_session(p1, "battlehall")
        self.assertIsNotNone(s1)
        self.assertEqual(s1["challenge_level"], 6)
        self.assertEqual(s1["printed_type"], "Normal")

        # Verify new mode sessions can now be added alongside old session without collision!
        db.save_session(p1, "battletower", None, 50, 1, "lobby_tower", "active")
        s1_hall = db.get_session(p1, "battlehall")
        s1_tower = db.get_session(p1, "battletower")
        self.assertIsNotNone(s1_hall)
        self.assertIsNotNone(s1_tower)
        self.assertEqual(s1_hall["mode"], "battlehall")
        self.assertEqual(s1_tower["mode"], "battletower")

        # Verify new tables exist and are functional
        v_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('battle_tower_records', 'factory_records');")
        tables = [r[0] for r in v_cur.fetchall()]
        self.assertIn("battle_tower_records", tables)
        self.assertIn("factory_records", tables)

        verify_conn.close()

        # Test idempotency: running init_db() again should not error or alter data
        db.init_db()
        s1_tower_after = db.get_session(p1, "battletower")
        self.assertIsNotNone(s1_tower_after)
        self.assertEqual(s1_tower_after["challenge_level"], 1)

    # =========================================================================
    # 4. Streak Boundary Values: 0 to 100+, Resets, PB Retention
    # =========================================================================
    def test_tower_streak_boundaries_and_pb_retention(self):
        """
        Verify streak progression from 0 to 120+, resets, PB retention, and re-climb past PB.
        """
        self._create_isolated_db("test_streak_boundaries")
        p = db.get_or_create_player("tower_climber", "TowerClimber")

        # Initial state
        rec = db.get_tower_record(p, "singles")
        self.assertEqual(rec, {"current_streak": 0, "max_streak": 0})

        # Climb from 0 to 125
        for expected in range(1, 126):
            val = db.increment_tower_streak(p, "singles")
            self.assertEqual(val, expected)

        rec = db.get_tower_record(p, "singles")
        self.assertEqual(rec, {"current_streak": 125, "max_streak": 125})

        # Defeat reset: current_streak becomes 0, max_streak stays 125
        db.reset_tower_streak(p, "singles")
        rec = db.get_tower_record(p, "singles")
        self.assertEqual(rec, {"current_streak": 0, "max_streak": 125})

        # Re-climb up to 50: PB remains 125
        for expected in range(1, 51):
            val = db.increment_tower_streak(p, "singles")
            self.assertEqual(val, expected)

        rec = db.get_tower_record(p, "singles")
        self.assertEqual(rec, {"current_streak": 50, "max_streak": 125})

        # Re-climb to 125: PB remains 125
        for expected in range(51, 126):
            val = db.increment_tower_streak(p, "singles")
            self.assertEqual(val, expected)

        rec = db.get_tower_record(p, "singles")
        self.assertEqual(rec, {"current_streak": 125, "max_streak": 125})

        # Breaking PB: climb to 126
        val = db.increment_tower_streak(p, "singles")
        self.assertEqual(val, 126)
        rec = db.get_tower_record(p, "singles")
        self.assertEqual(rec, {"current_streak": 126, "max_streak": 126})

        # Format independence: doubles is untouched
        rec_d = db.get_tower_record(p, "doubles")
        self.assertEqual(rec_d, {"current_streak": 0, "max_streak": 0})

        db.increment_tower_streak(p, "doubles")
        rec_d = db.get_tower_record(p, "doubles")
        self.assertEqual(rec_d, {"current_streak": 1, "max_streak": 1})

        rec_s = db.get_tower_record(p, "singles")
        self.assertEqual(rec_s, {"current_streak": 126, "max_streak": 126})

        # get_tower_all_records returns both
        all_recs = db.get_tower_all_records(p)
        self.assertEqual(all_recs["singles"], {"current_streak": 126, "max_streak": 126})
        self.assertEqual(all_recs["doubles"], {"current_streak": 1, "max_streak": 1})

        # reset_tower_all for singles format only
        db.reset_tower_all(p, "singles")
        self.assertEqual(db.get_tower_record(p, "singles"), {"current_streak": 0, "max_streak": 0})
        self.assertEqual(db.get_tower_record(p, "doubles"), {"current_streak": 1, "max_streak": 1})

        # reset_tower_all for all formats
        db.reset_tower_all(p)
        self.assertEqual(db.get_tower_record(p, "doubles"), {"current_streak": 0, "max_streak": 0})

    def test_factory_streak_boundaries_and_total_wins(self):
        """
        Verify factory records track current_streak, max_streak, and cumulative total_wins.
        """
        self._create_isolated_db("test_factory_boundaries")
        p = db.get_or_create_player("factory_pro", "FactoryPro")

        self.assertEqual(db.get_factory_record(p), {"current_streak": 0, "max_streak": 0, "total_wins": 0})

        for i in range(1, 41):
            res = db.increment_factory_streak(p)
            self.assertEqual(res, i)

        rec = db.get_factory_record(p)
        self.assertEqual(rec, {"current_streak": 40, "max_streak": 40, "total_wins": 40})

        # Reset streak
        db.reset_factory_streak(p)
        rec = db.get_factory_record(p)
        self.assertEqual(rec, {"current_streak": 0, "max_streak": 40, "total_wins": 40})

        # Next run of 15 wins
        for i in range(1, 16):
            res = db.increment_factory_streak(p)
            self.assertEqual(res, i)

        rec = db.get_factory_record(p)
        self.assertEqual(rec, {"current_streak": 15, "max_streak": 40, "total_wins": 55})

        # Full reset
        db.reset_factory_all(p)
        self.assertEqual(db.get_factory_record(p), {"current_streak": 0, "max_streak": 0, "total_wins": 0})

    def test_foreign_key_enforcement_on_records(self):
        """Verify database enforces foreign key integrity when inserting records for non-existent players."""
        self._create_isolated_db("test_fk_enforcement")
        with self.assertRaises(sqlite3.IntegrityError):
            db.increment_tower_streak(player_id=999999, format_type="singles")

    def test_connection_leak_on_exception_causes_database_lock(self):
        """
        DEFECT CONFIRMATION:
        Demonstrates that db helper functions (increment_tower_streak, increment_factory_streak,
        save_session, etc.) lack `try...finally: conn.close()` error handling. When an exception
        occurs, the unclosed SQLite connection retains an exclusive transaction lock, causing
        subsequent database calls to hang for 5s and fail with OperationalError: database is locked.
        """
        self._create_isolated_db("test_defect_connection_leak")
        # Step 1: Trigger an IntegrityError
        try:
            db.increment_tower_streak(player_id=999999, format_type="singles")
        except sqlite3.IntegrityError:
            pass

        # Step 2: Attempt a subsequent valid database operation
        # Due to leaked connection holding an active transaction lock, this raises OperationalError: database is locked
        with self.assertRaises(sqlite3.OperationalError, msg="Database is locked due to leaked connection from preceding exception"):
            db.get_or_create_player("user_after_leak", "UserAfterLeak")


if __name__ == "__main__":
    unittest.main()
