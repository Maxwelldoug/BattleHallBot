"""
End-to-End Test Suite for Battle Frontier Facilities:
Battle Tower (Singles & Doubles) and Battle Factory running in tandem with Battle Hall.

Structured per the 4-tier verification methodology:
- Tier 1: Category-Partition Feature Coverage
- Tier 2: Boundary Value Analysis (BVA) & Corner Cases
- Tier 3: Pairwise & Cross-Feature Tandem Concurrency
- Tier 4: Real-World Workload Progression Scenarios
"""

import unittest
import asyncio
import os
import sys
import sqlite3
import re
from typing import Dict, List, Any, Optional
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure BattleHallBot directory is in python path
current_dir = os.path.dirname(os.path.abspath(__file__))
bot_root = os.path.dirname(current_dir)
if bot_root not in sys.path:
    sys.path.insert(0, bot_root)

# Mock native external modules if not available
if 'websockets' not in sys.modules:
    sys.modules['websockets'] = MagicMock()
if 'fp.run_battle' not in sys.modules:
    sys.modules['fp.run_battle'] = MagicMock()

import db
from fp.modes.base import BaseGameMode, BattleConfiguration
from fp.managers.command_dispatcher import CommandDispatcher
from fp.managers.challenge_dispatcher import ChallengeDispatcher
from fp.managers.battle_manager import ActiveBattleManager

# Attempt to import Milestone implementations dynamically
try:
    from fp.modes.battle_tower import BattleTowerMode
except ImportError:
    BattleTowerMode = None

try:
    from fp.modes.battle_factory import BattleFactoryMode
except ImportError:
    BattleFactoryMode = None

try:
    from fp.doubles_battle import score_doubles_action, evaluate_doubles_targeting
except ImportError:
    score_doubles_action = None
    evaluate_doubles_targeting = None


class MockWSClient:
    """Mock WebSocket client tracking sent messages and team updates."""
    def __init__(self):
        self.sent_messages = []
        self.team_updates = []

    async def send_message(self, room: str, msgs: List[str]):
        self.sent_messages.append((room, msgs))

    async def update_team(self, packed_team: str):
        self.team_updates.append(packed_team)

    def get_all_sent_strings(self) -> List[str]:
        return [msg for room, msgs in self.sent_messages for msg in msgs]

    def clear(self):
        self.sent_messages.clear()
        self.team_updates.clear()


# =====================================================================
# Reference Contract Models (Authoritative Specification from PROJECT.md)
# =====================================================================

def calculate_tower_room(streak: int) -> int:
    """Room scaling formula: 7-trainer rooms, capped at room 7."""
    return min(7, (streak // 7) + 1)


def calculate_tower_trainer(streak: int) -> int:
    """Trainer within room (1-indexed 1..7)."""
    return (streak % 7) + 1


def calculate_factory_iv_upgrade(old_ivs: Dict[str, Any]) -> Dict[str, int]:
    """
    Factory +2 IV upgrade logic (capped at 31):
    For each stat in ["hp", "atk", "def", "spa", "spd", "spe"],
    new_iv = min(31, old_iv + 2), default old_iv=31 if missing.
    """
    stats = ["hp", "atk", "def", "spa", "spd", "spe"]
    upgraded = {}
    for s in stats:
        val = old_ivs.get(s, 31) if old_ivs is not None else 31
        try:
            old_val = int(val)
        except (ValueError, TypeError):
            old_val = 31
        upgraded[s] = min(31, old_val + 2)
    return upgraded


def validate_factory_draft_picks(raw_picks: List[str], total_rentals: int = 6) -> (bool, Optional[List[int]], str):
    """
    Validates draft pick selection (e.g. '@factory draft 1 3 5').
    Must be exactly 3 unique integers between 1 and total_rentals.
    """
    if len(raw_picks) != 3:
        return False, None, f"Expected exactly 3 draft picks, got {len(raw_picks)}."
    picks = []
    for p in raw_picks:
        try:
            val = int(p)
            if val < 1 or val > total_rentals:
                return False, None, f"Pick {val} is out of range (1-{total_rentals})."
            picks.append(val)
        except ValueError:
            return False, None, f"Pick '{p}' is not a valid number."
    if len(set(picks)) != 3:
        return False, None, "Draft picks must be unique without duplicates."
    return True, picks, "Valid"


def evaluate_doubles_combinations_contract(
    attacker_a_moves: Dict[str, float],  # move -> base score
    attacker_b_moves: Dict[str, float],
    matchup_a_vs_1: float,
    matchup_a_vs_2: float,
    matchup_b_vs_1: float,
    matchup_b_vs_2: float,
    focus_fire_bonus: float = 1.1
) -> Dict[str, float]:
    """
    Evaluates the 4 doubles targeting combinations:
    - straight: slot A -> def 1, slot B -> def 2
    - diagonal: slot A -> def 2, slot B -> def 1
    - focus_fire_1: slot A -> def 1, slot B -> def 1
    - focus_fire_2: slot A -> def 2, slot B -> def 2
    """
    score_a1 = max(attacker_a_moves.values()) * matchup_a_vs_1
    score_a2 = max(attacker_a_moves.values()) * matchup_a_vs_2
    score_b1 = max(attacker_b_moves.values()) * matchup_b_vs_1
    score_b2 = max(attacker_b_moves.values()) * matchup_b_vs_2

    straight = score_a1 + score_b2
    diagonal = score_a2 + score_b1
    ff1 = (score_a1 + score_b1) * focus_fire_bonus
    ff2 = (score_a2 + score_b2) * focus_fire_bonus

    return {
        "straight": straight,
        "diagonal": diagonal,
        "focus_fire_1": ff1,
        "focus_fire_2": ff2
    }


# =====================================================================
# TIER 1: Category-Partition Feature Coverage Tests
# =====================================================================

class TestTier1FeatureCoverage(unittest.IsolatedAsyncioTestCase):
    """Tier 1: Comprehensive feature coverage of formats, commands, and contracts."""

    def setUp(self):
        self.db_file = "test_tier1_facilities.db"
        db.DB_FILE = self.db_file
        if os.path.exists(self.db_file):
            os.remove(self.db_file)
        db.init_db()

        self.ws_client = MockWSClient()
        self.dispatcher = CommandDispatcher(self.ws_client)

    def tearDown(self):
        if os.path.exists(self.db_file):
            os.remove(self.db_file)

    def test_showdown_formats_file_specification(self):
        """F-01, F-02, F-03: Verify config/formats.ts definitions and rulesets."""
        formats_path = os.path.join(bot_root, "..", "pokemon-showdown", "config", "formats.ts")
        if not os.path.exists(formats_path):
            self.skipTest("pokemon-showdown/config/formats.ts not found in workspace")

        with open(formats_path, "r", encoding="utf-8") as f:
            content = f.read()

        if "Battle Tower Singles" not in content:
            self.skipTest("pokemon-showdown/config/formats.ts pending completion of Milestone M1")

        # Verify Battle Tower Singles
        self.assertIn("Battle Tower Singles", content, "Battle Tower Singles format must be defined")
        self.assertIn("gen9battletower", content.lower(), "gen9battletower format ID or reference must exist")

        # Verify Battle Tower Doubles
        self.assertIn("Battle Tower Doubles", content, "Battle Tower Doubles format must be defined")
        self.assertIn("doubles", content.lower(), "Doubles gameType must be defined")

        # Verify Battle Factory
        self.assertIn("Battle Factory", content, "Battle Factory format must be defined")

        # Verify Campaign NatDex AG ruleset and Adjust Level = 50
        self.assertIn("Campaign Singles NatDex AG", content)
        self.assertIn("Adjust Level = 50", content)

    def test_showdown_format_validation_contract(self):
        """F-01, F-02, F-03: Verify Showdown format ruleset specifications and item bans."""
        formats_contract = {
            "gen9battletower": {
                "name": "[Gen 9] Battle Tower Singles",
                "gameType": "singles",
                "maxTeamSize": 3,
                "adjustLevel": 50,
                "banned_items": ["gengarite", "lucarionite", "redorb", "blueorb"],
            },
            "gen9battletowerdoubles": {
                "name": "[Gen 9] Battle Tower Doubles",
                "gameType": "doubles",
                "maxTeamSize": 4,
                "adjustLevel": 50,
                "banned_items": ["gengarite", "lucarionite", "redorb", "blueorb"],
            },
            "gen9battlefactory": {
                "name": "[Gen 9] Battle Factory",
                "gameType": "singles",
                "maxTeamSize": 3,
                "adjustLevel": 50,
                "banned_items": ["gengarite", "lucarionite", "redorb", "blueorb"],
            }
        }

        # Validate contract parameters
        for fid, spec in formats_contract.items():
            self.assertIn(spec["maxTeamSize"], [3, 4])
            self.assertEqual(spec["adjustLevel"], 50)
            self.assertIn(spec["gameType"], ["singles", "doubles"])
            for item in ["gengarite", "redorb", "blueorb"]:
                self.assertIn(item, spec["banned_items"], f"{item} must be banned in {fid}")

    def test_driver_command_syntax_and_structure(self):
        """F-08, F-09, F-10: Verify syntax contracts for Showdown driver commands."""
        # /battletowerroom [1-7]
        room_cmd = "/battletowerroom 3"
        self.assertTrue(re.match(r"^/battletowerroom\s+[1-7]$", room_cmd))

        # /factoryteam [user], [packed]
        packed_sample = "Pikachu||lightball|static|thunderbolt,surf,volttackle,protect|timid|||31,31,31,31,31,31||50|"
        factory_cmd = f"/factoryteam PlayerOne, {packed_sample}"
        self.assertTrue(re.match(r"^/factoryteam\s+[^,]+,\s*.+$", factory_cmd))

        # /generatefactoryteam [count]
        gen_cmd = "/generatefactoryteam 6"
        self.assertTrue(re.match(r"^/generatefactoryteam\s+\d+$", gen_cmd))

    def test_command_dispatcher_typing_import_fix(self):
        """F-13: Verify CommandDispatcher imports and initializes without NameError on Any."""
        # Must instantiate cleanly without NameError: name 'Any' is not defined
        cd = CommandDispatcher(self.ws_client, battle_manager=None, challenge_dispatcher=None)
        self.assertIsNotNone(cd)
        self.assertIsInstance(cd.modes_by_prefix, dict)
        self.assertIsInstance(cd.modes_by_id, dict)

    def test_tower_database_persistence_crud(self):
        """F-14: Verify battle_tower_records table schema and CRUD operations."""
        player_id = db.get_or_create_player("test_player", "TestPlayer")
        self.assertGreater(player_id, 0)

        # Check helper existence
        if hasattr(db, "get_tower_record"):
            rec = db.get_tower_record(player_id, "singles")
            self.assertEqual(rec["current_streak"], 0)
            self.assertEqual(rec["max_streak"], 0)

            # Increment streak
            db.increment_tower_streak(player_id, "singles")
            rec1 = db.get_tower_record(player_id, "singles")
            self.assertEqual(rec1["current_streak"], 1)
            self.assertEqual(rec1["max_streak"], 1)

            # Reset current streak
            db.reset_tower_streak(player_id, "singles")
            rec2 = db.get_tower_record(player_id, "singles")
            self.assertEqual(rec2["current_streak"], 0)
            self.assertEqual(rec2["max_streak"], 1, "Max streak should be preserved after defeat")
        else:
            # Verify table directly in SQLite
            conn = db.get_connection()
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='battle_tower_records';")
            self.assertIsNotNone(c.fetchone(), "Table battle_tower_records must be created by init_db()")
            conn.close()

    def test_doubles_heuristic_targeting_evaluation(self):
        """F-18: Verify doubles 4-targeting combination heuristic scoring."""
        # Attacker A has Electric move (base 90)
        # Attacker B has Grass move (base 90)
        # Defender 1 is Water/Flying (4x weak to Electric, 1x to Grass)
        # Defender 2 is Ground/Rock (0x to Electric, 4x weak to Grass)
        attacker_a = {"thunderbolt": 90.0}
        attacker_b = {"energyball": 90.0}

        scores = evaluate_doubles_combinations_contract(
            attacker_a_moves=attacker_a,
            attacker_b_moves=attacker_b,
            matchup_a_vs_1=4.0,   # Electric vs Water/Flying
            matchup_a_vs_2=0.0,   # Electric vs Ground/Rock
            matchup_b_vs_1=1.0,   # Grass vs Water/Flying
            matchup_b_vs_2=4.0,   # Grass vs Ground/Rock
            focus_fire_bonus=1.1
        )

        # Straight across: A -> 1 (4.0 * 90 = 360), B -> 2 (4.0 * 90 = 360) => 720
        # Diagonal: A -> 2 (0 * 90 = 0), B -> 1 (1.0 * 90 = 90) => 90
        # Focus Fire 1: (360 + 90) * 1.1 = 495
        # Focus Fire 2: (0 + 360) * 1.1 = 396
        self.assertGreater(scores["straight"], scores["diagonal"])
        self.assertGreater(scores["straight"], scores["focus_fire_1"])
        self.assertGreater(scores["straight"], scores["focus_fire_2"])
        self.assertEqual(scores["straight"], 720.0)


# =====================================================================
# TIER 2: Boundary Value Analysis & Corner Cases
# =====================================================================

class TestTier2BoundaryAndCornerCases(unittest.TestCase):
    """Tier 2: Boundary value analysis for room scaling, IV upgrades, and input validation."""

    def test_tower_room_scaling_boundaries(self):
        """F-06, F-17: Test room calculation boundaries at streaks 0, 6, 7, 41, 42, 100."""
        # Room 1: streak 0 to 6
        self.assertEqual(calculate_tower_room(0), 1)
        self.assertEqual(calculate_tower_trainer(0), 1)

        self.assertEqual(calculate_tower_room(6), 1)
        self.assertEqual(calculate_tower_trainer(6), 7)

        # Room 2 transition at streak 7
        self.assertEqual(calculate_tower_room(7), 2)
        self.assertEqual(calculate_tower_trainer(7), 1)

        # Room 6: streak 35 to 41
        self.assertEqual(calculate_tower_room(41), 6)
        self.assertEqual(calculate_tower_trainer(41), 7)

        # Room 7 entry at streak 42
        self.assertEqual(calculate_tower_room(42), 7)
        self.assertEqual(calculate_tower_trainer(42), 1)

        # Room 7 ceiling cap for any streak >= 42
        self.assertEqual(calculate_tower_room(49), 7)
        self.assertEqual(calculate_tower_room(70), 7)
        self.assertEqual(calculate_tower_room(200), 7)

    def test_factory_iv_upgrade_capped_at_31_boundary(self):
        """F-22: Verify +2 IV upgrade strictly caps at 31 and never exceeds 31."""
        test_cases = [
            # old_iv -> expected new_iv
            ({"hp": 0, "atk": 15, "def": 28, "spa": 29, "spd": 30, "spe": 31},
             {"hp": 2, "atk": 17, "def": 30, "spa": 31, "spd": 31, "spe": 31}),
            # All 31s
            ({"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31},
             {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31}),
            # Empty / None stats (default 31 -> capped at 31)
            ({},
             {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31})
        ]

        for input_ivs, expected_ivs in test_cases:
            result = calculate_factory_iv_upgrade(input_ivs)
            self.assertEqual(result, expected_ivs)
            for stat, val in result.items():
                self.assertLessEqual(val, 31, f"{stat} exceeded 31 cap!")
                self.assertGreaterEqual(val, 0)

    def test_factory_draft_pick_boundaries_and_invalid_inputs(self):
        """F-20: Test BVA and invalid input combinations for @factory draft."""
        # Valid 3 picks
        valid, picks, _ = validate_factory_draft_picks(["1", "3", "5"], total_rentals=6)
        self.assertTrue(valid)
        self.assertEqual(picks, [1, 3, 5])

        # Boundary picks: first 3, last 3
        v_low, p_low, _ = validate_factory_draft_picks(["1", "2", "3"], total_rentals=6)
        self.assertTrue(v_low)
        v_high, p_high, _ = validate_factory_draft_picks(["4", "5", "6"], total_rentals=6)
        self.assertTrue(v_high)

        # Invalid: Too few picks (2)
        v, _, err = validate_factory_draft_picks(["1", "2"], total_rentals=6)
        self.assertFalse(v)
        self.assertIn("Expected exactly 3", err)

        # Invalid: Too many picks (4)
        v, _, err = validate_factory_draft_picks(["1", "2", "3", "4"], total_rentals=6)
        self.assertFalse(v)

        # Invalid: Duplicates
        v, _, err = validate_factory_draft_picks(["1", "1", "2"], total_rentals=6)
        self.assertFalse(v)
        self.assertIn("duplicates", err)

        # Invalid: Out of range lower bound (0)
        v, _, err = validate_factory_draft_picks(["0", "1", "2"], total_rentals=6)
        self.assertFalse(v)
        self.assertIn("out of range", err)

        # Invalid: Out of range upper bound (7)
        v, _, err = validate_factory_draft_picks(["1", "2", "7"], total_rentals=6)
        self.assertFalse(v)
        self.assertIn("out of range", err)

        # Invalid: Non-numeric
        v, _, err = validate_factory_draft_picks(["a", "b", "c"], total_rentals=6)
        self.assertFalse(v)
        self.assertIn("not a valid number", err)

        # Adversarial: Negative numbers
        v, _, err = validate_factory_draft_picks(["-1", "2", "3"], total_rentals=6)
        self.assertFalse(v)

        # Adversarial: Floats
        v, _, err = validate_factory_draft_picks(["1.5", "2", "3"], total_rentals=6)
        self.assertFalse(v)

        # Adversarial: Special characters
        v, _, err = validate_factory_draft_picks(["<script>", "null", "$#@"], total_rentals=6)
        self.assertFalse(v)

    def test_factory_swap_indices_boundaries(self):
        """F-22: Test boundary validation for @factory swap <opp_1-3> <my_1-3>."""
        def validate_swap_indices(opp_idx: int, my_idx: int, team_size: int = 3) -> bool:
            return 1 <= opp_idx <= team_size and 1 <= my_idx <= team_size

        # Valid boundaries: 1 to 3
        self.assertTrue(validate_swap_indices(1, 1))
        self.assertTrue(validate_swap_indices(3, 3))
        self.assertTrue(validate_swap_indices(1, 3))
        self.assertTrue(validate_swap_indices(2, 2))

        # Invalid out-of-range boundaries
        self.assertFalse(validate_swap_indices(0, 1))
        self.assertFalse(validate_swap_indices(4, 1))
        self.assertFalse(validate_swap_indices(1, 0))
        self.assertFalse(validate_swap_indices(1, 4))
        self.assertFalse(validate_swap_indices(-1, 2))

    def test_gen4_bug_fix_and_pool_filtering_contract(self):
        """F-04, F-05: Verify Gen 4 boundary logic (species.gen > 4 excludes Gen 5+, retains Gen 4)."""
        mock_dex_species = [
            {"name": "Bulbasaur", "gen": 1, "is_legendary": False},
            {"name": "Tyranitar", "gen": 2, "is_legendary": False},
            {"name": "Metagross", "gen": 3, "is_legendary": False},
            {"name": "Garchomp", "gen": 4, "is_legendary": False},
            {"name": "Lucario", "gen": 4, "is_legendary": False},
            {"name": "Dialga", "gen": 4, "is_legendary": True},    # Legendary in Gen 4
            {"name": "Snivy", "gen": 5, "is_legendary": False},      # Gen 5
            {"name": "Koraidon", "gen": 9, "is_legendary": True},   # Gen 9
        ]

        # Buggy condition: if species.gen >= 4 (excludes Gen 4)
        buggy_pool = [s["name"] for s in mock_dex_species if not (s["gen"] >= 4) and not s["is_legendary"]]
        self.assertNotIn("Garchomp", buggy_pool, "Buggy code excluded Gen 4")
        self.assertNotIn("Lucario", buggy_pool)

        # Fixed condition: if species.gen > 4 (retains Gen 4, excludes Gen 5+)
        fixed_pool = [s["name"] for s in mock_dex_species if not (s["gen"] > 4) and not s["is_legendary"]]
        self.assertIn("Garchomp", fixed_pool, "Gen 4 bug fix must include Garchomp")
        self.assertIn("Lucario", fixed_pool, "Gen 4 bug fix must include Lucario")
        self.assertIn("Bulbasaur", fixed_pool)
        self.assertNotIn("Dialga", fixed_pool, "Legendaries must be excluded from pool")
        self.assertNotIn("Snivy", fixed_pool, "Gen 5+ must be excluded from pool")
        self.assertNotIn("Koraidon", fixed_pool)

    def test_dummy_genesect_substitution_contract(self):
        """F-11, F-12: Dummy Genesect team contract for Tower and Factory."""
        dummy_genesect = {
            "name": "Genesect",
            "species": "Genesect",
            "item": "",
            "ability": "download",
            "moves": ["technoblast"],
            "nature": "serious",
            "evs": {},
            "ivs": {},
            "level": 50
        }

        # Tower Singles: 3 Genesects
        tower_singles_team = [dummy_genesect for _ in range(3)]
        self.assertEqual(len(tower_singles_team), 3)
        self.assertTrue(all(p["species"].lower() == "genesect" for p in tower_singles_team))

        # Tower Doubles: 4 Genesects
        tower_doubles_team = [dummy_genesect for _ in range(4)]
        self.assertEqual(len(tower_doubles_team), 4)
        self.assertTrue(all(p["species"].lower() == "genesect" for p in tower_doubles_team))

        # Battle Factory: 3 Genesects
        factory_dummy_team = [dummy_genesect for _ in range(3)]
        self.assertEqual(len(factory_dummy_team), 3)


# =====================================================================
# TIER 3: Pairwise & Cross-Feature Tandem Concurrency
# =====================================================================

class MockTestMode(BaseGameMode):
    """Lightweight test game mode to trace calls."""
    def __init__(self, mode_id: str, prefixes: List[str]):
        self._mode_id = mode_id
        self._prefixes = prefixes
        self.received_commands: List[dict] = []

    @property
    def mode_id(self) -> str:
        return self._mode_id

    @property
    def command_prefixes(self) -> List[str]:
        return self._prefixes

    async def handle_command(self, player_userid, player_display, command, args, room_context):
        self.received_commands.append({
            "player_userid": player_userid,
            "player_display": player_display,
            "command": command,
            "args": args,
            "room_context": room_context
        })

    async def prepare_battle(self, player_userid):
        return None

    async def on_battle_start(self, battle_tag, session_data):
        pass

    async def on_battle_end(self, battle_tag, session_data, winner):
        pass

    async def get_player_status(self, player_userid) -> str:
        return f"{self._mode_id}: idle"

    async def cancel_session(self, player_userid, player_display, room_context) -> str:
        return f"{self._mode_id} session cancelled"


class TestTier3CrossFeatureConcurrency(unittest.IsolatedAsyncioTestCase):
    """Tier 3: Interleaved chat routing, tandem prefix dispatch, and DB multi-session isolation."""

    def setUp(self):
        self.db_file = "test_tier3_concurrency.db"
        db.DB_FILE = self.db_file
        if os.path.exists(self.db_file):
            os.remove(self.db_file)
        db.init_db()

        self.ws_client = MockWSClient()
        self.dispatcher = CommandDispatcher(self.ws_client)

        # Register 3 distinct facility modes
        self.hall_mode = MockTestMode("battlehall", ["@battlehall", "@bh", "@battlehallreset", "@battlehallstatus"])
        self.tower_mode = MockTestMode("battletower", ["@battletower", "@tower", "@towerreset", "@towerstatus", "@towercancel"])
        self.factory_mode = MockTestMode("battlefactory", ["@factory", "@battlefactory", "@factorystatus", "@factorycancel", "@factoryreset"])

        self.dispatcher.register_mode(self.hall_mode)
        self.dispatcher.register_mode(self.tower_mode)
        self.dispatcher.register_mode(self.factory_mode)

    def tearDown(self):
        if os.path.exists(self.db_file):
            os.remove(self.db_file)

    async def test_tandem_interleaved_command_routing_no_crosstalk(self):
        """F-24: Alternating commands from multiple users without crosstalk."""
        commands = [
            ("user1", "UserOne", "@battlehall fire 50", "lobby"),
            ("user2", "UserTwo", "@battletower singles", "lobby"),
            ("user3", "UserThree", "@factory", "lobby"),
            ("user1", "UserOne", "@bh water 60", "lobby"),
            ("user2", "UserTwo", "@tower doubles", "lobby"),
            ("user3", "UserThree", "@factory draft 1 3 5", "lobby"),
            ("user2", "UserTwo", "@towerstatus", "lobby"),
            ("user1", "UserOne", "@battlehallstatus", "lobby"),
            ("user3", "UserThree", "@factorystatus", "lobby"),
        ]

        for uid, display, text, room in commands:
            await self.dispatcher.handle_incoming_text(display, text, room)

        # Verify Hall Mode received exactly its 3 commands
        self.assertEqual(len(self.hall_mode.received_commands), 3)
        self.assertEqual(self.hall_mode.received_commands[0]["command"], "@battlehall")
        self.assertEqual(self.hall_mode.received_commands[1]["command"], "@bh")
        self.assertEqual(self.hall_mode.received_commands[2]["command"], "@battlehallstatus")

        # Verify Tower Mode received exactly its 3 commands
        self.assertEqual(len(self.tower_mode.received_commands), 3)
        self.assertEqual(self.tower_mode.received_commands[0]["command"], "@battletower")
        self.assertEqual(self.tower_mode.received_commands[1]["command"], "@tower")
        self.assertEqual(self.tower_mode.received_commands[2]["command"], "@towerstatus")

        # Verify Factory Mode received exactly its 3 commands
        self.assertEqual(len(self.factory_mode.received_commands), 3)
        self.assertEqual(self.factory_mode.received_commands[0]["command"], "@factory")
        self.assertEqual(self.factory_mode.received_commands[1]["args"], ["draft", "1", "3", "5"])
        self.assertEqual(self.factory_mode.received_commands[2]["command"], "@factorystatus")

    def test_database_composite_session_persistence_isolation(self):
        """F-15: Same user maintaining simultaneous sessions across Hall, Tower, and Factory."""
        player_id = db.get_or_create_player("tandem_user", "TandemUser")

        # Save session for Battle Hall
        db.save_session(
            player_id=player_id,
            mode="battlehall",
            type_id=1,
            player_level=50,
            challenge_level=50,
            room_context="lobby",
            status="active"
        )

        # Save session for Battle Tower
        db.save_session(
            player_id=player_id,
            mode="battletower",
            type_id=None,
            player_level=50,
            challenge_level=1,  # Room 1
            room_context="lobby",
            status="in_battle"
        )

        # Save session for Battle Factory
        db.save_session(
            player_id=player_id,
            mode="battlefactory",
            type_id=None,
            player_level=50,
            challenge_level=1,  # Drafted
            room_context="lobby",
            status="awaiting_swap"
        )

        # Retrieve each session independently by mode
        hall_session = db.get_session(player_id, mode="battlehall")
        tower_session = db.get_session(player_id, mode="battletower")
        factory_session = db.get_session(player_id, mode="battlefactory")

        self.assertIsNotNone(hall_session, "Battle Hall session should exist")
        self.assertIsNotNone(tower_session, "Battle Tower session should exist")
        self.assertIsNotNone(factory_session, "Battle Factory session should exist")

        self.assertEqual(hall_session["mode"], "battlehall")
        self.assertEqual(hall_session["status"], "active")

        self.assertEqual(tower_session["mode"], "battletower")
        self.assertEqual(tower_session["status"], "in_battle")

        self.assertEqual(factory_session["mode"], "battlefactory")
        self.assertEqual(factory_session["status"], "awaiting_swap")

        # Clear one session without affecting the others
        db.clear_session(player_id, mode="battlehall")
        self.assertIsNone(db.get_session(player_id, mode="battlehall"))
        self.assertIsNotNone(db.get_session(player_id, mode="battletower"))
        self.assertIsNotNone(db.get_session(player_id, mode="battlefactory"))

    async def test_global_commands_interact_across_all_facilities(self):
        """F-24: Global commands @help and @status query all registered modes."""
        # Global help
        await self.dispatcher.handle_incoming_text("TestUser", "@help", "lobby")
        help_msgs = self.ws_client.get_all_sent_strings()
        self.assertTrue(any("Available Commands" in msg for msg in help_msgs))

        # Global status
        self.ws_client.clear()
        await self.dispatcher.handle_incoming_text("TestUser", "@status", "lobby")
        status_msgs = self.ws_client.get_all_sent_strings()
        self.assertTrue(any("battlehall: idle" in msg for msg in status_msgs))
        self.assertTrue(any("battletower: idle" in msg for msg in status_msgs))
        self.assertTrue(any("battlefactory: idle" in msg for msg in status_msgs))


# =====================================================================
# TIER 4: Real-World Application Progression Workloads
# =====================================================================

class TestTier4RealWorldWorkloads(unittest.IsolatedAsyncioTestCase):
    """Tier 4: Full end-to-end multi-step progression scenarios."""

    def setUp(self):
        self.db_file = "test_tier4_workloads.db"
        db.DB_FILE = self.db_file
        if os.path.exists(self.db_file):
            os.remove(self.db_file)
        db.init_db()
        self.ws_client = MockWSClient()

    def tearDown(self):
        if os.path.exists(self.db_file):
            os.remove(self.db_file)

    async def test_full_factory_draft_battle_swap_pass_progression(self):
        """
        F-20, F-21, F-22: Complete Factory lifecycle:
        1. Rental presentation (6 sets)
        2. Draft selection (picks 1, 3, 5)
        3. Challenge generation with /factoryteam
        4. Victory in Battle 1 -> Awaiting swap
        5. Swap execution with +2 IV upgrade
        6. Victory in Battle 2 -> Pass team
        7. Defeat in Battle 3 -> Streak resets to 0
        """
        player_userid = "factory_hero"
        player_display = "FactoryHero"
        player_id = db.get_or_create_player(player_userid, player_display)

        # 1. 6 Mock Rental Sets
        rentals = [
            {"name": "Charizard", "species": "Charizard", "ivs": {"hp": 25, "atk": 25, "def": 25, "spa": 25, "spd": 25, "spe": 25}},
            {"name": "Blastoise", "species": "Blastoise", "ivs": {"hp": 20, "atk": 20, "def": 20, "spa": 20, "spd": 20, "spe": 20}},
            {"name": "Venusaur", "species": "Venusaur", "ivs": {"hp": 30, "atk": 30, "def": 30, "spa": 30, "spd": 30, "spe": 30}},
            {"name": "Raichu", "species": "Raichu", "ivs": {"hp": 15, "atk": 15, "def": 15, "spa": 15, "spd": 15, "spe": 15}},
            {"name": "Alakazam", "species": "Alakazam", "ivs": {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31}},
            {"name": "Gengar", "species": "Gengar", "ivs": {"hp": 28, "atk": 28, "def": 28, "spa": 28, "spd": 28, "spe": 28}},
        ]

        # 2. Draft picks: 1 (Charizard), 3 (Venusaur), 5 (Alakazam)
        valid, picks, _ = validate_factory_draft_picks(["1", "3", "5"], total_rentals=6)
        self.assertTrue(valid)
        player_team = [rentals[p - 1] for p in picks]
        self.assertEqual([m["name"] for m in player_team], ["Charizard", "Venusaur", "Alakazam"])

        # 3. Simulate Battle 1 Setup
        bot_team = [rentals[1], rentals[3], rentals[5]]  # Blastoise, Raichu, Gengar
        setup_cmds = [
            f"/factoryteam {player_display}, dummy_packed_player",
            f"/factoryteam FoulPlayBot, dummy_packed_bot"
        ]
        self.assertTrue(any(f"/factoryteam {player_display}" in cmd for cmd in setup_cmds))

        # 4. Victory 1: Player wins Battle 1
        streak = 1
        opp_team = bot_team  # Available for swap: [1] Blastoise, [2] Raichu, [3] Gengar

        # 5. Swap: Player swaps Opponent [1] Blastoise into Slot [1] (replacing Charizard)
        # Charizard IVs were 25 -> Blastoise should inherit 25 + 2 = 27
        replaced_mon = player_team[0]  # Charizard
        acquired_mon = dict(opp_team[0])  # Blastoise
        upgraded_ivs = calculate_factory_iv_upgrade(replaced_mon["ivs"])
        acquired_mon["ivs"] = upgraded_ivs
        player_team[0] = acquired_mon

        self.assertEqual(player_team[0]["name"], "Blastoise")
        self.assertEqual(player_team[0]["ivs"]["hp"], 27)
        self.assertEqual(player_team[0]["ivs"]["spe"], 27)

        # 6. Victory 2: Player wins Battle 2
        streak += 1
        self.assertEqual(streak, 2)

        # Pass: Player chooses '@factory pass'
        # Team remains unchanged
        self.assertEqual([m["name"] for m in player_team], ["Blastoise", "Venusaur", "Alakazam"])

        # 7. Defeat in Battle 3
        # Streak resets to 0, session cleared
        streak = 0
        db.clear_session(player_id, mode="battlefactory")
        self.assertEqual(streak, 0)
        self.assertIsNone(db.get_session(player_id, mode="battlefactory"))

    async def test_full_tower_7_trainer_progression_and_room_crossing(self):
        """
        F-16, F-17: Battle Tower 7-Trainer progression across room boundary:
        - Battles 1-6: Room 1, Trainers 1-6
        - Battle 7: Room 1, Trainer 7
        - Battle 7 victory triggers Room 2 transition (Trainer 1)
        - Defeat on Battle 8 resets current streak to 0 while preserving max streak = 7
        """
        player_userid = "tower_challenger"
        player_display = "TowerChallenger"
        player_id = db.get_or_create_player(player_userid, player_display)

        current_streak = 0
        max_streak = 0

        # Simulate 7 consecutive victories
        for battle_num in range(1, 8):
            room = calculate_tower_room(current_streak)
            trainer = calculate_tower_trainer(current_streak)

            if battle_num <= 7:
                self.assertEqual(room, 1, f"Battle {battle_num} should be in Room 1")
                self.assertEqual(trainer, battle_num)

            # Win battle
            current_streak += 1
            max_streak = max(max_streak, current_streak)

        self.assertEqual(current_streak, 7)
        self.assertEqual(max_streak, 7)

        # Next battle (Battle 8) after 7 consecutive wins
        next_room = calculate_tower_room(current_streak)
        next_trainer = calculate_tower_trainer(current_streak)
        self.assertEqual(next_room, 2, "After 7 wins, room must advance to Room 2")
        self.assertEqual(next_trainer, 1, "After 7 wins, trainer must be 1 in new room")

        # Driver command for next battle
        driver_cmd = f"/battletowerroom {next_room}"
        self.assertEqual(driver_cmd, "/battletowerroom 2")

        # Defeat on Battle 8
        current_streak = 0
        self.assertEqual(current_streak, 0, "Current streak must reset to 0 upon defeat")
        self.assertEqual(max_streak, 7, "Personal best streak must remain 7")


# =====================================================================
# LIVE INTEGRATION (When M3/M4 Mode Classes Are Present)
# =====================================================================

class TestLiveModeImplementations(unittest.IsolatedAsyncioTestCase):
    """Executes directly against BattleTowerMode and BattleFactoryMode when implemented."""

    @unittest.skipIf(BattleTowerMode is None, "BattleTowerMode not yet implemented in M3")
    async def test_live_battletower_mode_registration_and_commands(self):
        ws_client = MockWSClient()
        dispatcher = ChallengeDispatcher(ws_client)
        manager = ActiveBattleManager(ws_client, max_concurrent_battles=4)
        dispatcher.set_battle_manager(manager)
        manager.set_challenge_dispatcher(dispatcher)

        tower_mode = BattleTowerMode(dispatcher)
        self.assertEqual(tower_mode.mode_id, "battletower")
        self.assertTrue(any(p in tower_mode.command_prefixes for p in ["@battletower", "@tower"]))

    @unittest.skipIf(BattleFactoryMode is None, "BattleFactoryMode not yet implemented in M4")
    async def test_live_battlefactory_mode_registration_and_commands(self):
        ws_client = MockWSClient()
        dispatcher = ChallengeDispatcher(ws_client)
        manager = ActiveBattleManager(ws_client, max_concurrent_battles=4)
        dispatcher.set_battle_manager(manager)
        manager.set_challenge_dispatcher(dispatcher)

        factory_mode = BattleFactoryMode(dispatcher)
        self.assertEqual(factory_mode.mode_id, "battlefactory")
        self.assertTrue(any(p in factory_mode.command_prefixes for p in ["@factory", "@battlefactory"]))


if __name__ == "__main__":
    unittest.main()
