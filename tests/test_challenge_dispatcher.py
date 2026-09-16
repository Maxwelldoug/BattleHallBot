import unittest
import asyncio
import sys
from unittest.mock import MagicMock

# Mock native modules
sys.modules['fp.run_battle'] = MagicMock()
sys.modules['websockets'] = MagicMock()

from fp.modes.base import BaseGameMode, BattleConfiguration
from fp.managers.challenge_dispatcher import ChallengeDispatcher
from fp.managers.battle_manager import ActiveBattleManager


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
        pass

    async def on_battle_end(self, battle_tag, session_data, winner):
        pass

    async def get_player_status(self, player_userid):
        return "status"

    async def cancel_session(self, player_userid, player_display, room_context):
        return "cancelled"


class MockWSClient:
    def __init__(self):
        self.sent_messages = []
        self.team_updates = []

    async def send_message(self, room, msgs):
        self.sent_messages.append((room, msgs))

    async def update_team(self, packed_team):
        self.team_updates.append(packed_team)


class TestChallengeDispatcher(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ws_client = MockWSClient()
        self.dispatcher = ChallengeDispatcher(self.ws_client)
        self.manager = ActiveBattleManager(self.ws_client, max_concurrent_battles=2)
        self.dispatcher.set_battle_manager(self.manager)
        self.manager.set_challenge_dispatcher(self.dispatcher)
        self.mode = MockMode()

    async def test_challenge_dispatch_sequence(self):
        config = BattleConfiguration(
            pokemon_format="gen9battlehall",
            team_packed="PACKED_GENESECT_TEAM",
            setup_commands=["/battlehalllevel 50", "/battlehalltype fire"],
            extra_info={}
        )

        success = await self.dispatcher.dispatch_challenge(
            player_userid="max",
            player_display="Max",
            mode=self.mode,
            battle_config=config,
            room_context="lobby",
            session_data={"level": 50}
        )
        self.assertTrue(success)

        # Check order of commands sent:
        # 1. Setup commands
        # 2. Team update
        # 3. /challenge command
        sent_commands = [msg for room, msgs in self.ws_client.sent_messages for msg in msgs]
        self.assertIn("/battlehalllevel 50", sent_commands)
        self.assertIn("/battlehalltype fire", sent_commands)
        self.assertIn("PACKED_GENESECT_TEAM", self.ws_client.team_updates)
        self.assertIn("/challenge Max,gen9battlehall", sent_commands)

        # Check pending challenge was registered
        pending = self.manager.get_pending_challenge("max")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["player_display"], "Max")

    async def test_capacity_queuing(self):
        # Fill capacity
        from fp.managers.battle_manager import BattleInstance
        inst1 = BattleInstance("b1", "u1", "U1", self.mode, {}, BattleConfiguration("gen9mock"), "lobby")
        inst2 = BattleInstance("b2", "u2", "U2", self.mode, {}, BattleConfiguration("gen9mock"), "lobby")
        self.manager.battles_by_room["b1"] = inst1
        self.manager.battles_by_room["b2"] = inst2

        config = BattleConfiguration("gen9mock")
        # Dispatch 3rd challenge (should be enqueued)
        success = await self.dispatcher.dispatch_challenge(
            player_userid="queued_user",
            player_display="QueuedUser",
            mode=self.mode,
            battle_config=config,
            room_context="lobby",
            session_data={}
        )
        self.assertTrue(success)
        self.assertEqual(len(self.manager.wait_queue), 1)
        self.assertEqual(self.manager.wait_queue[0].player_userid, "queued_user")

        # Cleanup timeout task
        self.manager.remove_from_wait_queue("queued_user")


if __name__ == "__main__":
    unittest.main()
