import unittest
import asyncio
import os
import sys
from unittest.mock import MagicMock, AsyncMock

# Mock native modules
sys.modules['fp.run_battle'] = MagicMock()
sys.modules['websockets'] = MagicMock()

import db
from fp import run_battle
from fp.modes.battle_hall import BattleHallMode
from fp.managers.challenge_dispatcher import ChallengeDispatcher
from fp.managers.battle_manager import ActiveBattleManager
from fp.managers.command_dispatcher import CommandDispatcher


class MockWSClient:
    def __init__(self):
        self.pending_battles_queue = asyncio.Queue()
        self.room_queues = {}
        self.room_buffers = {}
        self.sent_messages = []
        self.left_battles = []
        self.team_updates = []

    async def send_message(self, room, msgs):
        self.sent_messages.append((room, msgs))

    async def update_team(self, packed_team):
        self.team_updates.append(packed_team)

    async def leave_battle(self, battle_tag, timeout=5.0):
        self.left_battles.append(battle_tag)

    async def receive_message(self, room=""):
        await asyncio.sleep(0.01)
        return ""

    def push_back_message(self, msg, room=""):
        pass


class TestMultiBattleFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.DB_FILE = "test_multi_battle_flow.db"
        if os.path.exists(db.DB_FILE):
            os.remove(db.DB_FILE)
        db.init_db()

        self.ws_client = MockWSClient()
        self.challenge_dispatcher = ChallengeDispatcher(self.ws_client)
        # Set max_concurrent_battles to 2 to test capacity queueing
        self.battle_manager = ActiveBattleManager(self.ws_client, max_concurrent_battles=2)
        self.challenge_dispatcher.set_battle_manager(self.battle_manager)
        self.battle_manager.set_challenge_dispatcher(self.challenge_dispatcher)

        self.command_dispatcher = CommandDispatcher(
            self.ws_client,
            battle_manager=self.battle_manager,
            challenge_dispatcher=self.challenge_dispatcher
        )
        self.hall_mode = BattleHallMode(self.challenge_dispatcher)
        self.command_dispatcher.register_mode(self.hall_mode)

    def tearDown(self):
        # Clean up any waiting queues
        for qp in list(self.battle_manager.wait_queue):
            if qp.timeout_task and not qp.timeout_task.done():
                qp.timeout_task.cancel()
        if os.path.exists(db.DB_FILE):
            os.remove(db.DB_FILE)

    async def test_three_players_concurrent_challenges_and_queue(self):
        # Player 1 (Alice) sends @battlehall fire 50
        await self.command_dispatcher.handle_incoming_text("Alice", "@battlehall fire 50", "lobby")
        self.assertTrue(self.battle_manager.has_active_battle_or_challenge("alice"))
        self.assertEqual(self.battle_manager.active_battles_count(), 0) # Pending, not started room yet

        # Player 2 (Bob) sends @battlehall water 50
        await self.command_dispatcher.handle_incoming_text("Bob", "@battlehall water 50", "lobby")
        self.assertTrue(self.battle_manager.has_active_battle_or_challenge("bob"))

        # Simulate Alice and Bob's rooms opening
        run_battle.pokemon_battle = AsyncMock(return_value="alice")

        # Manually trigger battle start for Alice and Bob
        pending_alice = self.battle_manager.pending_challenges.pop("alice")
        inst_alice = self.battle_manager.battles_by_room["battle-alice"] = self.battle_manager.battles_by_player["alice"] = (
            self.battle_manager._make_instance("battle-alice", pending_alice)
            if hasattr(self.battle_manager, "_make_instance")
            else None
        )
        from fp.managers.battle_manager import BattleInstance
        inst_alice = BattleInstance(
            "battle-alice", "alice", "Alice", self.hall_mode, pending_alice["session_data"],
            pending_alice["battle_config"], "lobby"
        )
        self.battle_manager.battles_by_room["battle-alice"] = inst_alice
        self.battle_manager.battles_by_player["alice"] = inst_alice

        pending_bob = self.battle_manager.pending_challenges.pop("bob")
        inst_bob = BattleInstance(
            "battle-bob", "bob", "Bob", self.hall_mode, pending_bob["session_data"],
            pending_bob["battle_config"], "lobby"
        )
        self.battle_manager.battles_by_room["battle-bob"] = inst_bob
        self.battle_manager.battles_by_player["bob"] = inst_bob

        # At this point, active battles count is 2 (at capacity!)
        self.assertEqual(self.battle_manager.active_battles_count(), 2)
        self.assertTrue(self.battle_manager.is_at_capacity())

        # Player 3 (Charlie) sends @battlehall grass 50
        # Because capacity is full (2/2), Charlie must be placed into the wait queue!
        await self.command_dispatcher.handle_incoming_text("Charlie", "@battlehall grass 50", "lobby")
        self.assertEqual(len(self.battle_manager.wait_queue), 1)
        self.assertEqual(self.battle_manager.wait_queue[0].player_userid, "charlie")

        # Verify Charlie received queue notification
        sent = [msg for room, msgs in self.ws_client.sent_messages for msg in msgs]
        self.assertTrue(any("Position #1" in s for s in sent))

        # Now simulate Bob's battle completing (Bob lost)
        await self.hall_mode.on_battle_end("battle-bob", inst_bob.session_data, winner="FoulPlayBot")
        self.battle_manager.battles_by_room.pop("battle-bob")
        self.battle_manager.battles_by_player.pop("bob")

        # Slot is now available! Advancing queue...
        await self.battle_manager.process_next_in_queue()

        # Charlie should now be popped from the queue and challenged!
        self.assertEqual(len(self.battle_manager.wait_queue), 0)
        self.assertTrue(self.battle_manager.has_active_battle_or_challenge("charlie"))
        sent_commands = [msg for room, msgs in self.ws_client.sent_messages for msg in msgs]
        self.assertTrue(any("/challenge Charlie,gen9battlehall" in s for s in sent_commands))


if __name__ == "__main__":
    unittest.main()
