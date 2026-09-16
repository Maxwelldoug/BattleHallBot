import unittest
import sys
from unittest.mock import MagicMock, AsyncMock

# Mock native modules
sys.modules['fp.run_battle'] = MagicMock()
sys.modules['websockets'] = MagicMock()

from fp.modes.base import BaseGameMode, BattleConfiguration
from fp.managers.command_dispatcher import CommandDispatcher


class MockMode(BaseGameMode):
    def __init__(self, mode_id, prefixes):
        self._mode_id = mode_id
        self._prefixes = prefixes
        self.handled_commands = []

    @property
    def mode_id(self):
        return self._mode_id

    @property
    def command_prefixes(self):
        return self._prefixes

    async def handle_command(self, player_userid, player_display, command, args, room_context):
        self.handled_commands.append((player_userid, player_display, command, args, room_context))

    async def prepare_battle(self, player_userid):
        return None

    async def on_battle_start(self, battle_tag, session_data):
        pass

    async def on_battle_end(self, battle_tag, session_data, winner):
        pass

    async def get_player_status(self, player_userid):
        return f"{self._mode_id}: active"

    async def cancel_session(self, player_userid, player_display, room_context):
        return "cancelled"


class MockWSClient:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, room, msgs):
        self.sent_messages.append((room, msgs))


class TestCommandDispatcher(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ws_client = MockWSClient()
        self.dispatcher = CommandDispatcher(self.ws_client)
        self.hall_mode = MockMode("battlehall", ["@battlehall", "@bh"])
        self.tower_mode = MockMode("battletower", ["@battletower", "@bt"])
        self.dispatcher.register_mode(self.hall_mode)
        self.dispatcher.register_mode(self.tower_mode)

    async def test_mode_command_routing(self):
        # Route @battlehall
        await self.dispatcher.handle_incoming_text("Max", "@battlehall fire 50", "lobby")
        self.assertEqual(len(self.hall_mode.handled_commands), 1)
        self.assertEqual(self.hall_mode.handled_commands[0][2], "@battlehall")
        self.assertEqual(self.hall_mode.handled_commands[0][3], ["fire", "50"])

        # Route alias @bh
        await self.dispatcher.handle_incoming_text("Max", "@bh water 60", "lobby")
        self.assertEqual(len(self.hall_mode.handled_commands), 2)
        self.assertEqual(self.hall_mode.handled_commands[1][2], "@bh")

        # Route @battletower
        await self.dispatcher.handle_incoming_text("Max", "@battletower singles", "lobby")
        self.assertEqual(len(self.tower_mode.handled_commands), 1)
        self.assertEqual(self.tower_mode.handled_commands[0][2], "@battletower")

    async def test_global_help_and_status(self):
        # Global help
        await self.dispatcher.handle_incoming_text("Max", "@help", "lobby")
        self.assertTrue(any("Available Commands" in str(msg) for room, msg in self.ws_client.sent_messages))

        # Global status
        self.ws_client.sent_messages.clear()
        await self.dispatcher.handle_incoming_text("Max", "@status", "lobby")
        sent = [msg for room, msgs in self.ws_client.sent_messages for msg in msgs]
        self.assertTrue(any("battlehall: active" in s for s in sent))
        self.assertTrue(any("battletower: active" in s for s in sent))


if __name__ == "__main__":
    unittest.main()
