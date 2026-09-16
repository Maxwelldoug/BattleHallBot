import unittest
import asyncio
import sys
from unittest.mock import MagicMock, AsyncMock

# Mock native modules
mock_run_battle = MagicMock()
sys.modules['fp.run_battle'] = mock_run_battle
sys.modules['websockets'] = MagicMock()
import fp
fp.run_battle = mock_run_battle

import db
from fp.modes.base import BaseGameMode, BattleConfiguration
from fp.managers.battle_manager import ActiveBattleManager, BattleInstance, QueuedPlayer


class MockMode(BaseGameMode):
    @property
    def mode_id(self):
        return "mock_mode"

    @property
    def command_prefixes(self):
        return ["@mock"]

    async def handle_command(self, player_userid, player_display, command, args, room_context):
        pass

    async def prepare_battle(self, player_userid):
        return BattleConfiguration(pokemon_format="gen9mock")

    async def on_battle_start(self, battle_tag, session_data):
        session_data["start_called"] = True

    async def on_battle_end(self, battle_tag, session_data, winner):
        session_data["end_called"] = True
        session_data["winner"] = winner

    async def get_player_status(self, player_userid):
        return "status"

    async def cancel_session(self, player_userid, player_display, room_context):
        return "cancelled"


class MockClient:
    def __init__(self):
        self.pending_battles_queue = asyncio.Queue()
        self.room_queues = {}
        self.room_buffers = {}
        self.sent_messages = []
        self.left_battles = []

    async def send_message(self, room, msgs):
        self.sent_messages.append((room, msgs))

    async def leave_battle(self, battle_tag, timeout=5.0):
        self.left_battles.append(battle_tag)

    async def receive_message(self, room=""):
        await asyncio.sleep(0.01)
        return "|title|Alice vs. FoulPlayBot"

    def push_back_message(self, msg, room=""):
        pass


class TestActiveBattleManager(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from config import FoulPlayConfig
        self.orig_username = getattr(FoulPlayConfig, "username", None)
        FoulPlayConfig.username = "FoulPlayBot"
        self.client = MockClient()
        self.manager = ActiveBattleManager(self.client, max_concurrent_battles=2)
        self.mode = MockMode()

    def tearDown(self):
        from config import FoulPlayConfig
        if self.orig_username is not None:
            FoulPlayConfig.username = self.orig_username

    def test_capacity_and_counts(self):
        self.assertEqual(self.manager.active_battles_count(), 0)
        self.assertFalse(self.manager.is_at_capacity())

        # Add 1 battle
        inst1 = BattleInstance(
            battle_tag="battle-1",
            player_userid="player1",
            player_display="Player1",
            mode=self.mode,
            session_data={},
            battle_config=BattleConfiguration("gen9mock"),
            room_context="lobby"
        )
        self.manager.battles_by_room["battle-1"] = inst1
        self.manager.battles_by_player["player1"] = inst1

        self.assertEqual(self.manager.active_battles_count(), 1)
        self.assertFalse(self.manager.is_at_capacity())
        self.assertTrue(self.manager.has_active_battle_or_challenge("player1"))
        self.assertFalse(self.manager.has_active_battle_or_challenge("player2"))

        # Add 2nd battle (reaches capacity)
        inst2 = BattleInstance(
            battle_tag="battle-2",
            player_userid="player2",
            player_display="Player2",
            mode=self.mode,
            session_data={},
            battle_config=BattleConfiguration("gen9mock"),
            room_context="lobby"
        )
        self.manager.battles_by_room["battle-2"] = inst2
        self.manager.battles_by_player["player2"] = inst2

        self.assertEqual(self.manager.active_battles_count(), 2)
        self.assertTrue(self.manager.is_at_capacity())

    def test_wait_queue_fifo(self):
        pos1 = self.manager.enqueue_wait(
            "player1", "Player1", self.mode, BattleConfiguration("gen9mock"), "lobby", {}
        )
        self.assertEqual(pos1, 1)

        pos2 = self.manager.enqueue_wait(
            "player2", "Player2", self.mode, BattleConfiguration("gen9mock"), "lobby", {}
        )
        self.assertEqual(pos2, 2)

        self.assertEqual(len(self.manager.wait_queue), 2)
        self.assertEqual(self.manager.wait_queue[0].player_userid, "player1")
        self.assertEqual(self.manager.wait_queue[1].player_userid, "player2")

        # Test removal from queue
        removed = self.manager.remove_from_wait_queue("player1")
        self.assertTrue(removed)
        self.assertEqual(len(self.manager.wait_queue), 1)
        self.assertEqual(self.manager.wait_queue[0].player_userid, "player2")

        # Cleanup timeout tasks
        self.manager.remove_from_wait_queue("player2")

    async def test_unsolicited_battle_handling(self):
        # Start orchestrator
        self.manager.start_orchestrator()

        # Enqueue unexpected battle
        await self.client.pending_battles_queue.put(
            ("battle-gen9battlehall-unsolicited", ">battle-gen9battlehall-unsolicited\n|init|battle\n|title|Bob vs. FoulPlayBot")
        )

        await asyncio.sleep(0.05)
        await self.manager.stop_orchestrator()

        # Verify rejection message was sent and room was left
        self.assertTrue(any("Sorry, I am currently busy" in str(msg) for room, msg in self.client.sent_messages))
        self.assertIn("battle-gen9battlehall-unsolicited", self.client.left_battles)

    async def test_pending_challenge_and_lifecycle(self):
        session_data = {"player_userid": "alice"}
        config = BattleConfiguration("gen9mock")
        self.manager.register_pending_challenge(
            player_userid="alice",
            player_display="Alice",
            mode=self.mode,
            battle_config=config,
            room_context="lobby",
            session_data=session_data,
            challenge_sent_time=100.0
        )
        self.assertTrue(self.manager.has_active_battle_or_challenge("alice"))

        # Emulate pokemon_battle returning "alice" as winner
        import fp.run_battle
        fp.run_battle.pokemon_battle = AsyncMock(return_value="alice")

        inst = BattleInstance(
            battle_tag="battle-gen9mock-1",
            player_userid="alice",
            player_display="Alice",
            mode=self.mode,
            session_data=session_data,
            battle_config=config,
            room_context="lobby"
        )
        self.manager.battles_by_room["battle-gen9mock-1"] = inst
        self.manager.battles_by_player["alice"] = inst

        await self.manager._run_battle_lifecycle(inst)

        # Verify lifecycle hooks were called
        self.assertTrue(session_data.get("start_called"))
        self.assertTrue(session_data.get("end_called"))
        self.assertEqual(session_data.get("winner"), "alice")

        # Verify registries were cleaned up
        self.assertNotIn("battle-gen9mock-1", self.manager.battles_by_room)
        self.assertNotIn("alice", self.manager.battles_by_player)


if __name__ == "__main__":
    unittest.main()
