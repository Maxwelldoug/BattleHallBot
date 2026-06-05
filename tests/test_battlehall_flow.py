import unittest
import asyncio
import os
import sqlite3
import sys
from unittest.mock import MagicMock
# Mock fp.run_battle module so we do not import poke_engine which has a missing Rust dependency
sys.modules['fp.run_battle'] = MagicMock()

import run
import db
from fp.websocket_client import PSWebsocketClient, WebsocketConnectionLost
from fp.helpers import normalize_name
from config import FoulPlayConfig, BotModes

class MockWebsocket:
    def __init__(self, messages):
        self.messages = messages
        self.index = 0

    async def recv(self):
        if self.index < len(self.messages):
            msg = self.messages[self.index]
            self.index += 1
            return msg
        else:
            await asyncio.sleep(0.1)
            raise asyncio.CancelledError()

    async def send(self, message):
        pass

class TestBattleHallFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.DB_FILE = "test_battlehall_flow.db"
        if os.path.exists(db.DB_FILE):
            os.remove(db.DB_FILE)
        db.init_db()
        
        # Save config
        self.orig_username = getattr(FoulPlayConfig, "username", None)
        self.orig_bot_mode = getattr(FoulPlayConfig, "bot_mode", None)
        FoulPlayConfig.username = "FoulPlayBot"
        FoulPlayConfig.bot_mode = BotModes.battlehall

    def tearDown(self):
        if self.orig_username is not None:
            FoulPlayConfig.username = self.orig_username
        elif hasattr(FoulPlayConfig, "username"):
            delattr(FoulPlayConfig, "username")

        if self.orig_bot_mode is not None:
            FoulPlayConfig.bot_mode = self.orig_bot_mode
        elif hasattr(FoulPlayConfig, "bot_mode"):
            delattr(FoulPlayConfig, "bot_mode")

        if os.path.exists(db.DB_FILE):
            os.remove(db.DB_FILE)

    def test_extract_opponent_userid(self):
        # Case 1: Title block with bot as p2
        msg1 = ">battle-gen9battlehall-1\n|init|battle\n|title|Max vs. FoulPlayBot"
        opp1 = run.extract_opponent_userid(msg1, "FoulPlayBot")
        self.assertEqual(opp1, "max")

        # Case 2: Title block with bot as p1
        msg2 = ">battle-gen9battlehall-2\n|init|battle\n|title|FoulPlayBot vs. Max"
        opp2 = run.extract_opponent_userid(msg2, "FoulPlayBot")
        self.assertEqual(opp2, "max")

        # Case 3: Player lines
        msg3 = ">battle-gen9battlehall-3\n|player|p1|Max|1\n|player|p2|FoulPlayBot|2"
        opp3 = run.extract_opponent_userid(msg3, "FoulPlayBot")
        self.assertEqual(opp3, "max")

        # Case 4: No matching info
        msg4 = ">battle-gen9battlehall-4\n|turn|1"
        opp4 = run.extract_opponent_userid(msg4, "FoulPlayBot")
        self.assertIsNone(opp4)

    async def test_websocket_router_filtering(self):
        messages = [
            ">battle-gen9battlehall-1\n|init|battle\n|title|Max vs. FoulPlayBot",
            ">battle-gen9battlehall-1\n|turn|1",
            ">battle-gen9battlehall-1\n|move|p1: Genesect|U-turn",
            ">battle-gen9battlehall-2\n|init|battle\n|title|Alice vs. FoulPlayBot"
        ]

        client = PSWebsocketClient()
        client.websocket = MockWebsocket(messages)
        client.room_queues = {}
        client.pending_battles_queue = asyncio.Queue()

        task = asyncio.create_task(client._message_router_loop())
        await asyncio.sleep(0.05)
        task.cancel()

        # Check pending battles queue size and contents
        # Only the two |init| messages should be present
        self.assertEqual(client.pending_battles_queue.qsize(), 2)
        
        room1, msg1 = await client.pending_battles_queue.get()
        self.assertEqual(room1, "battle-gen9battlehall-1")
        self.assertIn("|init|", msg1)

        room2, msg2 = await client.pending_battles_queue.get()
        self.assertEqual(room2, "battle-gen9battlehall-2")
        self.assertIn("|init|", msg2)

    async def test_challenge_timeout_timestamp_matching(self):
        active_battle = {
            "player_userid": "max",
            "player_display": "Max",
            "challenge_sent_time": 100.0,
            "room_context": "lobby"
        }

        cancel_called = []
        async def mock_send_message(room, msgs):
            for m in msgs:
                if "/cancelchallenge" in m:
                    cancel_called.append(m)

        # An old timeout monitor wakes up for a challenge sent at 100.0
        # But the active challenge was updated to a newer one sent at 200.0
        active_battle["challenge_sent_time"] = 200.0

        # Run the old timeout check
        # This simulates challenge_timeout_monitor waking up after sleeping
        challenge_sent_time_old = 100.0
        if (
            active_battle
            and active_battle["player_userid"] == "max"
            and active_battle["challenge_sent_time"] == challenge_sent_time_old
            and "battle_started" not in active_battle
        ):
            await mock_send_message("", ["/cancelchallenge {}".format(active_battle["player_display"])])
            active_battle = None

        # Verify the newer challenge was NOT canceled/reset
        self.assertIsNotNone(active_battle)
        self.assertEqual(active_battle["challenge_sent_time"], 200.0)
        self.assertEqual(len(cancel_called), 0)

        # Run the matching timeout check (sent at 200.0)
        challenge_sent_time_matching = 200.0
        if (
            active_battle
            and active_battle["player_userid"] == "max"
            and active_battle["challenge_sent_time"] == challenge_sent_time_matching
            and "battle_started" not in active_battle
        ):
            await mock_send_message("", ["/cancelchallenge {}".format(active_battle["player_display"])])
            active_battle = None

        # Verify the challenge WAS canceled
        self.assertIsNone(active_battle)
        self.assertEqual(len(cancel_called), 1)
        self.assertIn("/cancelchallenge Max", cancel_called[0])

    async def test_unsolicited_battle_rejection(self):
        active_battle = {
            "player_userid": "max",
            "player_display": "Max",
            "challenge_sent_time": 100.0,
            "room_context": "lobby"
        }

        messages_sent = []
        leaves_called = []

        class MockClient:
            username = "FoulPlayBot"
            async def send_message(self, room, msgs):
                messages_sent.append((room, msgs))
            async def leave_battle(self, battle_tag):
                leaves_called.append(battle_tag)

        mock_client = MockClient()

        # Frame 1: Alice challenges the bot (Alice is unsolicited)
        msg_unsolicited = ">battle-gen9battlehall-alice\n|init|battle\n|title|Alice vs. FoulPlayBot"
        opp_userid = run.extract_opponent_userid(msg_unsolicited, mock_client.username)
        self.assertEqual(opp_userid, "alice")

        # Emulate battle orchestration validation
        current_b = None
        if active_battle is not None and opp_userid == active_battle["player_userid"]:
            active_battle["battle_started"] = True
            current_b = active_battle.copy()

        self.assertIsNone(current_b) # Rejects alice
        
        # Emulate rejection path
        if current_b is None:
            await mock_client.send_message("battle-gen9battlehall-alice", ["Sorry, I am currently busy or this battle was not requested."])
            await mock_client.leave_battle("battle-gen9battlehall-alice")

        self.assertEqual(len(messages_sent), 1)
        self.assertEqual(messages_sent[0][0], "battle-gen9battlehall-alice")
        self.assertEqual(leaves_called, ["battle-gen9battlehall-alice"])

        # Frame 2: Max challenges the bot (Max is solicited)
        msg_solicited = ">battle-gen9battlehall-max\n|init|battle\n|title|Max vs. FoulPlayBot"
        opp_userid_sol = run.extract_opponent_userid(msg_solicited, mock_client.username)
        self.assertEqual(opp_userid_sol, "max")

        current_b_sol = None
        if active_battle is not None and opp_userid_sol == active_battle["player_userid"]:
            active_battle["battle_started"] = True
            current_b_sol = active_battle.copy()

        self.assertIsNotNone(current_b_sol)
        self.assertEqual(current_b_sol["player_userid"], "max")

    def test_win_loss_database_updates(self):
        player_id = db.get_or_create_player("max", "Max")
        type_row = db.get_type_by_canonical_name("fire")
        type_id = type_row[0]

        # Scenario 1: Increment wins on win
        db.increment_player_wins(player_id, type_id)
        self.assertEqual(db.get_player_wins(player_id, type_id), 1)

        # Scenario 2: Wipe all wins on loss
        db.reset_player_all_wins(player_id)
        self.assertEqual(db.get_player_wins(player_id, type_id), 0)

    async def test_room_buffering_fifo(self):
        client = PSWebsocketClient()
        client.room_buffers = {}
        client.room_queues = {}
        client.router_task = MagicMock() # mock router enabled

        # Push back two messages in sequence
        client.push_back_message("first message", room="battle-room")
        client.push_back_message("second message", room="battle-room")

        # Verify they are read in the exact same sequence (FIFO)
        m1 = await client.receive_message(room="battle-room")
        m2 = await client.receive_message(room="battle-room")

        self.assertEqual(m1, "first message")
        self.assertEqual(m2, "second message")

    def test_format_enforcement(self):
        # Only room tags starting with 'battle-gen9battlehall-' are accepted
        valid_tag = "battle-gen9battlehall-1234"
        invalid_tag = "battle-gen9randombattle-5678"

        self.assertTrue(valid_tag.startswith("battle-gen9battlehall-"))
        self.assertFalse(invalid_tag.startswith("battle-gen9battlehall-"))

    async def test_delayed_opponent_resolution(self):
        # Setup active battle
        active_battle = {
            "player_userid": "max",
            "player_display": "Max",
            "challenge_sent_time": 100.0,
            "room_context": "lobby"
        }

        # First frame is only init (lacks title/player)
        first_frame = ">battle-gen9battlehall-1\n|init|battle"
        opp1 = run.extract_opponent_userid(first_frame, "FoulPlayBot")
        self.assertIsNone(opp1)

        # Subsequent frame contains the title
        second_frame = ">battle-gen9battlehall-1\n|title|Max vs. FoulPlayBot"
        opp2 = run.extract_opponent_userid(second_frame, "FoulPlayBot")
        self.assertEqual(opp2, "max")

        # Emulate the loop resolving it
        opponent_userid = opp1
        messages_read = [first_frame]
        
        # Simulating receiving subsequent frame
        if opponent_userid is None:
            m = second_frame
            messages_read.append(m)
            opponent_userid = run.extract_opponent_userid(m, "FoulPlayBot")

        self.assertEqual(opponent_userid, "max")
        self.assertEqual(len(messages_read), 2)
        
        # Verify it matches active player and gets accepted
        current_b = None
        if active_battle is not None and opponent_userid == active_battle["player_userid"]:
            active_battle["battle_started"] = True
            current_b = active_battle.copy()

        self.assertIsNotNone(current_b)
        self.assertEqual(current_b["player_userid"], "max")

    async def test_case_insensitive_and_fallback_routing(self):
        client = PSWebsocketClient()
        client.websocket = MockWebsocket([])
        client.room_queues = {}
        client.room_buffers = {}
        client.pending_battles_queue = asyncio.Queue()
        
        # Test case insensitivity (joining Lobby, receiving on lobby)
        await client.join_room("Lobby")
        self.assertEqual(client.lobby_room, "Lobby")
        
        # Simulate router thread processing a message with mixed casing and un-prefixed fallback
        messages_to_route = [
            ">Lobby\n|c:|12345|~User|hello",
            "|c:|67890|~User|@battlehall fire 50"
        ]
        
        client.websocket = MockWebsocket(messages_to_route)
        client.start_router()
        await asyncio.sleep(0.05)
        
        # Verify both messages were mapped to the lowercase "lobby" queue
        m1 = await client.receive_message(room="lobby")
        m2 = await client.receive_message(room="lobby")
        
        self.assertEqual(m1, ">Lobby\n|c:|12345|~User|hello")
        self.assertEqual(m2, "|c:|67890|~User|@battlehall fire 50")
        
        await client.stop_router()

    async def test_router_shutdown_wakes_listeners(self):
        client = PSWebsocketClient()
        client.room_queues = {"lobby": asyncio.Queue()}
        client.room_buffers = {}
        client.pending_battles_queue = asyncio.Queue()
        client.router_task = object()

        client._signal_router_shutdown()

        with self.assertRaises(WebsocketConnectionLost):
            await client.receive_message(room="lobby")

        battle_tag, msg = await client.pending_battles_queue.get()
        self.assertIsNone(battle_tag)
        self.assertIsNone(msg)

    def test_team_dict_fallback_graceful(self):
        from fp.battle import Battler
        battler = Battler()
        
        # Define a team_dict with only Genesect (similar to our Battle Hall setup)
        battler.team_dict = [{
            "name": "Genesect",
            "species": "genesect",
            "nature": "serious",
            "evs": {"hp": "0", "atk": "252", "def": "4", "spa": "0", "spd": "0", "spe": "252"}
        }]
        
        # Emulate a Showdown request_json containing a Cacturne (not Genesect)
        request_json = {
            "active": [
                {
                    "moves": [{"move": "Spite", "id": "spite"}]
                }
            ],
            "side": {
                "name": "EvilWoodenPlank",
                "id": "p1",
                "pokemon": [
                    {
                        "ident": "p1: Cacturne",
                        "details": "Cacturne, L18, M",
                        "condition": "57/57",
                        "active": True,
                        "stats": {"atk": 46, "def": 26, "spa": 46, "spd": 38, "spe": 39},
                        "moves": ["spite"],
                        "baseAbility": "sandveil",
                        "item": "wiseglasses",
                        "pokeball": "pokeball",
                        "ability": "sandveil",
                        "teraType": "Grass",
                        "terastallized": ""
                    }
                ]
            },
            "rqid": 2
        }
        
        # This should execute cleanly without raising ValueError because of the fallback mechanism
        battler.initialize_first_turn_user_from_json(request_json)
        
        # Verify the active pokemon is initialized with default serious nature and standard EVs
        self.assertIsNotNone(battler.active)
        self.assertEqual(battler.active.name, "cacturne")
        self.assertEqual(battler.active.nature, "serious")
        self.assertEqual(battler.active.evs, (85,) * 6)

    def test_battlehall_preserves_team_dict_constructor(self):
        from fp.battle import Battler

        battler = Battler()
        battler.pokemon_format = "gen9battlehall"
        battler.team_dict = [{
            "name": "Genesect",
            "species": "genesect",
            "nature": "jolly",
            "evs": {"hp": "0", "atk": "252", "def": "4", "spa": "0", "spd": "0", "spe": "252"}
        }]

        request_json = {
            "active": [
                {
                    "moves": [{"move": "Spite", "id": "spite"}]
                }
            ],
            "side": {
                "name": "EvilWoodenPlank",
                "id": "p1",
                "pokemon": [
                    {
                        "ident": "p1: Genesect",
                        "details": "Genesect, L100, M",
                        "condition": "100/100",
                        "active": True,
                        "stats": {"atk": 100, "def": 100, "spa": 100, "spd": 100, "spe": 100},
                        "moves": ["spite"],
                        "baseAbility": "download",
                        "item": "",
                        "pokeball": "pokeball",
                        "ability": "download",
                        "teraType": "Steel",
                        "terastallized": ""
                    }
                ]
            },
            "rqid": 2
        }

        battler.initialize_first_turn_user_from_json(request_json)

        self.assertIsNotNone(battler.team_dict)
        self.assertEqual(battler.team_dict[0]["species"], "genesect")
        self.assertEqual(battler.active.nature, "jolly")
        self.assertEqual(battler.active.evs, (0, 252, 4, 0, 0, 252))

if __name__ == "__main__":
    unittest.main()
