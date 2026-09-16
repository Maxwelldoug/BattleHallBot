import unittest
import os
import sys
from unittest.mock import MagicMock, AsyncMock

# Mock native modules
sys.modules['fp.run_battle'] = MagicMock()
sys.modules['websockets'] = MagicMock()

import db
from fp.modes.battle_hall import BattleHallMode, calculate_challenge_level
from fp.managers.challenge_dispatcher import ChallengeDispatcher
from fp.managers.battle_manager import ActiveBattleManager


class MockWSClient:
    def __init__(self):
        self.sent_messages = []
        self.team_updates = []

    async def send_message(self, room, msgs):
        self.sent_messages.append((room, msgs))

    async def update_team(self, packed_team):
        self.team_updates.append(packed_team)


class TestBattleHallMode(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.DB_FILE = "test_battlehall_mode.db"
        if os.path.exists(db.DB_FILE):
            os.remove(db.DB_FILE)
        db.init_db()

        self.ws_client = MockWSClient()
        self.dispatcher = ChallengeDispatcher(self.ws_client)
        self.manager = ActiveBattleManager(self.ws_client, max_concurrent_battles=4)
        self.dispatcher.set_battle_manager(self.manager)
        self.manager.set_challenge_dispatcher(self.dispatcher)
        self.mode = BattleHallMode(self.dispatcher)

    def tearDown(self):
        if os.path.exists(db.DB_FILE):
            os.remove(db.DB_FILE)

    def test_level_calculation(self):
        # Level 50, rank 1 (0 wins), 0 other types
        lvl = calculate_challenge_level(50, 0, 0)
        self.assertTrue(1 <= lvl <= 50)

        # Level 100, rank 10 (9 wins), 17 other types
        lvl_max = calculate_challenge_level(100, 9, 17)
        self.assertEqual(lvl_max, 97)

    async def test_command_validation_and_dispatch(self):
        # Test invalid type
        await self.mode.handle_command("player1", "Player1", "@battlehall", ["notatype", "50"], "lobby")
        self.assertTrue(any("Invalid type" in str(msg) for room, msg in self.ws_client.sent_messages))

        # Test invalid level
        self.ws_client.sent_messages.clear()
        await self.mode.handle_command("player1", "Player1", "@battlehall", ["fire", "150"], "lobby")
        self.assertTrue(any("Invalid level" in str(msg) for room, msg in self.ws_client.sent_messages))

        # Test valid command
        self.ws_client.sent_messages.clear()
        await self.mode.handle_command("player1", "Player1", "@battlehall", ["fire", "50"], "lobby")
        
        # Verify challenge was dispatched
        sent = [msg for room, msgs in self.ws_client.sent_messages for msg in msgs]
        self.assertTrue(any("/challenge Player1,gen9battlehall" in s for s in sent))
        self.assertTrue(any("/battlehalltype fire" in s for s in sent))

        # Verify DB session was persisted
        player = db.get_player_by_userid("player1")
        self.assertIsNotNone(player)
        session = db.get_session(player["id"], mode="battlehall")
        self.assertIsNotNone(session)
        self.assertEqual(session["canonical_type"], "fire")
        self.assertEqual(session["player_level"], 50)

    async def test_victory_auto_rematch_and_defeat_reset(self):
        # Start a challenge
        await self.mode.handle_command("player1", "Player1", "@battlehall", ["water", "50"], "lobby")
        player = db.get_player_by_userid("player1")
        player_id = player["id"]
        type_row = db.get_type_by_canonical_name("water")
        type_id = type_row[0]

        session_data = {
            "player_id": player_id,
            "player_userid": "player1",
            "type_id": type_id,
            "printed_type": "Water",
            "canonical_type": "water",
            "player_level": 50,
            "challenge_level": 50,
            "room_context": "lobby"
        }

        # Simulate battle started (removes from pending_challenges) and winning Rank 1
        self.manager.remove_pending_challenge("player1")
        self.ws_client.sent_messages.clear()
        await self.mode.on_battle_end("battle-gen9battlehall-1", session_data, winner="player1")

        # Check DB wins updated to 1
        self.assertEqual(db.get_player_wins(player_id, type_id), 1)
        self.assertEqual(db.get_player_total_wins(player_id), 1)

        # Check that auto-challenge for Rank 2 was dispatched
        sent = [msg for room, msgs in self.ws_client.sent_messages for msg in msgs]
        self.assertTrue(any("/challenge Player1,gen9battlehall" in s for s in sent))

        # Check match history was recorded
        history = db.get_match_history(player_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["result"], "win")

        # Now simulate a LOSS on Rank 2
        self.ws_client.sent_messages.clear()
        await self.mode.on_battle_end("battle-gen9battlehall-2", session_data, winner="bot")

        # Check DB wins reset to 0
        self.assertEqual(db.get_player_wins(player_id, type_id), 0)
        self.assertEqual(db.get_player_total_wins(player_id), 0)

        # Check session was cleared
        self.assertIsNone(db.get_session(player_id, mode="battlehall"))


if __name__ == "__main__":
    unittest.main()
