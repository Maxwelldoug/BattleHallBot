"""
Unit tests for Battle Tower mode, Battle Factory mode, and Doubles Heuristic Battler.
These tests exercise pure logic without requiring poke_engine or dateutil.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure BattleHallBot root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ──────────────────────────────────────────────────────────────────────────────
# Doubles Heuristic Battler tests
# ──────────────────────────────────────────────────────────────────────────────

class TestDoublesHeuristicBattler(unittest.TestCase):
    """Tests for DoublesHeuristicBattler logic."""

    def setUp(self):
        from fp.doubles_battle import DoublesHeuristicBattler
        from data import all_move_json, pokedex
        self.battler = DoublesHeuristicBattler(all_move_json, pokedex)

    def _make_request(self, moves_s0, moves_s1=None):
        """Build a minimal request JSON for two active slots."""
        active = [
            {"moves": [{"id": m, "pp": 15, "maxpp": 15, "disabled": False} for m in moves_s0]},
        ]
        if moves_s1 is not None:
            active.append(
                {"moves": [{"id": m, "pp": 15, "maxpp": 15, "disabled": False} for m in moves_s1]}
            )
        return {"rqid": 7, "active": active, "side": {"pokemon": []}}

    def test_fire_move_prefers_grass_over_water_target(self):
        """Flamethrower should score higher against Grass vs Water opponents."""
        from fp.doubles_battle import _type_effectiveness
        grass_eff = _type_effectiveness("fire", ["grass"])
        water_eff = _type_effectiveness("fire", ["water"])
        self.assertGreater(grass_eff, water_eff)

    def test_electric_vs_ground_is_zero(self):
        from fp.doubles_battle import _type_effectiveness
        self.assertEqual(_type_effectiveness("electric", ["ground"]), 0.0)

    def test_pick_move_returns_valid_format(self):
        """pick_move should return a list with one /choose string."""
        req = self._make_request(["flamethrower"], ["thunderbolt"])
        result = self.battler.pick_move(req, ["bulbasaur", "squirtle"], rqid=7)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith("/choose "))
        self.assertIn("|7", result[0])

    def test_pick_move_selects_super_effective(self):
        """Flamethrower should target Grass-type (super effective) over Water-type."""
        req = self._make_request(["flamethrower"], ["flamethrower"])
        # opp slot 0 = bulbasaur (grass), slot 1 = squirtle (water)
        result = self.battler.pick_move(req, ["bulbasaur", "squirtle"], rqid=1)
        choice = result[0]
        # Both slots should pick 1 (bulbasaur = opp slot 0 = target 1)
        # slot0 has flamethrower vs grass/poison bulbasaur — very effective
        # slot1 has flamethrower vs grass too
        self.assertIn(" 1", choice)

    def test_pick_move_handles_fainted_opp(self):
        """With only one live opponent, both slots should target it."""
        req = self._make_request(["watergun"], ["watergun"])
        result = self.battler.pick_move(req, [None, "charizard"], rqid=2)
        choice = result[0]
        # Only slot 2 (opp slot 1 = charizard) is live
        self.assertIn(" 2", choice)

    def test_pick_move_disabled_move_skipped(self):
        """Disabled moves should not be selected."""
        req = {
            "rqid": 3,
            "active": [
                {"moves": [
                    {"id": "flamethrower", "pp": 0, "maxpp": 15, "disabled": True},
                    {"id": "scratch", "pp": 15, "maxpp": 15, "disabled": False},
                ]},
            ],
            "side": {"pokemon": []},
        }
        result = self.battler.pick_move(req, ["squirtle", None], rqid=3)
        self.assertTrue(result[0].startswith("/choose "))
        # Should use move 2 (scratch), not disabled move 1
        self.assertIn("move 2", result[0])

    def test_spread_move_omits_target(self):
        """Surf (spread / allAdjacent) must NOT include a target number."""
        req = self._make_request(["surf"], ["surf"])
        result = self.battler.pick_move(req, ["charizard", "golem"], rqid=4)
        choice = result[0]
        self.assertEqual(choice, "/choose move 1, move 1|4")

    def test_only_status_moves_falls_back(self):
        """If all moves are status (basePower=0), fallback to move 1."""
        req = self._make_request(["willowisp"], ["willowisp"])
        result = self.battler.pick_move(req, ["bulbasaur", "charmander"], rqid=5)
        self.assertTrue(result[0].startswith("/choose "))

    def test_opp_species_update(self):
        """_update_opp_species should parse switch lines correctly."""
        from fp.doubles_battle import _update_opp_species
        opp = [None, None]
        msg = ">battle-gen9battletowerdoubles-1\n|switch|p2a: Bulbasaur|Bulbasaur, Lv50|100/100\n|switch|p2b: Charizard|Charizard, Lv50|100/100\n"
        _update_opp_species(msg, opp)
        self.assertEqual(opp[0], "bulbasaur")
        self.assertEqual(opp[1], "charizard")

    def test_opp_species_faint(self):
        """Fainted opponent slot should be set to None."""
        from fp.doubles_battle import _update_opp_species
        opp = ["bulbasaur", "charizard"]
        msg = "|faint|p2a: Bulbasaur\n"
        _update_opp_species(msg, opp)
        self.assertIsNone(opp[0])
        self.assertEqual(opp[1], "charizard")


# ──────────────────────────────────────────────────────────────────────────────
# Battle Tower DB helpers test
# ──────────────────────────────────────────────────────────────────────────────

class TestBattleTowerDB(unittest.TestCase):
    """Tests for battle_tower_records DB helpers."""

    def setUp(self):
        import db
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.DB_FILE = self.tmp.name
        db.init_db()
        self.player_id = db.get_or_create_player("towertest", "TowerTest")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_initial_record_is_zero(self):
        import db
        rec = db.get_tower_record(self.player_id, "singles")
        self.assertEqual(rec["current_streak"], 0)
        self.assertEqual(rec["max_streak"], 0)

    def test_increment_streak(self):
        import db
        for i in range(1, 8):
            streak = db.increment_tower_streak(self.player_id, "singles")
            self.assertEqual(streak, i)
        rec = db.get_tower_record(self.player_id, "singles")
        self.assertEqual(rec["current_streak"], 7)
        self.assertEqual(rec["max_streak"], 7)

    def test_reset_preserves_max(self):
        import db
        db.increment_tower_streak(self.player_id, "singles")
        db.increment_tower_streak(self.player_id, "singles")
        db.reset_tower_streak(self.player_id, "singles")
        rec = db.get_tower_record(self.player_id, "singles")
        self.assertEqual(rec["current_streak"], 0)
        self.assertEqual(rec["max_streak"], 2)

    def test_reset_all_clears_both(self):
        import db
        db.increment_tower_streak(self.player_id, "singles")
        db.increment_tower_streak(self.player_id, "doubles")
        db.reset_tower_all(self.player_id)
        records = db.get_tower_all_records(self.player_id)
        self.assertEqual(records["singles"]["current_streak"], 0)
        self.assertEqual(records["doubles"]["current_streak"], 0)

    def test_doubles_independent_of_singles(self):
        import db
        db.increment_tower_streak(self.player_id, "singles")
        db.increment_tower_streak(self.player_id, "singles")
        db.increment_tower_streak(self.player_id, "doubles")
        s_rec = db.get_tower_record(self.player_id, "singles")
        d_rec = db.get_tower_record(self.player_id, "doubles")
        self.assertEqual(s_rec["current_streak"], 2)
        self.assertEqual(d_rec["current_streak"], 1)


# ──────────────────────────────────────────────────────────────────────────────
# Battle Factory DB helpers test
# ──────────────────────────────────────────────────────────────────────────────

class TestBattleFactoryDB(unittest.TestCase):
    def setUp(self):
        import db
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.DB_FILE = self.tmp.name
        db.init_db()
        self.player_id = db.get_or_create_player("factorytest", "FactoryTest")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_initial_record(self):
        import db
        rec = db.get_factory_record(self.player_id)
        self.assertEqual(rec["current_streak"], 0)
        self.assertEqual(rec["max_streak"], 0)
        self.assertEqual(rec["total_wins"], 0)

    def test_increment_and_reset(self):
        import db
        for _ in range(5):
            db.increment_factory_streak(self.player_id)
        rec = db.get_factory_record(self.player_id)
        self.assertEqual(rec["current_streak"], 5)
        self.assertEqual(rec["total_wins"], 5)
        self.assertEqual(rec["max_streak"], 5)

        db.reset_factory_streak(self.player_id)
        rec = db.get_factory_record(self.player_id)
        self.assertEqual(rec["current_streak"], 0)
        self.assertEqual(rec["max_streak"], 5)  # max preserved
        self.assertEqual(rec["total_wins"], 5)  # total preserved


# ──────────────────────────────────────────────────────────────────────────────
# Battle Tower Mode unit tests (no live server)
# ──────────────────────────────────────────────────────────────────────────────

class TestBattleTowerMode(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import db
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.DB_FILE = self.tmp.name
        db.init_db()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _make_mode(self):
        from fp.modes.battle_tower import BattleTowerMode
        mock_cd = MagicMock()
        mock_cd.send_reply = AsyncMock()
        mock_cd.dispatch_challenge = AsyncMock(return_value=True)
        mock_cd.battle_manager = MagicMock()
        mock_cd.battle_manager.remove_from_wait_queue = MagicMock(return_value=False)
        mock_cd.battle_manager.cancel_pending_challenge = AsyncMock(return_value=False)
        return BattleTowerMode(mock_cd), mock_cd

    async def test_start_singles_challenge(self):
        import db
        from fp.modes.battle_tower import BATTLES_PER_ROOM, _room_for_streak
        mode, cd = self._make_mode()
        await mode.handle_command("testuser", "TestUser", "@battletower", ["single"], "testroom")
        self.assertTrue(cd.dispatch_challenge.called)
        kwargs = cd.dispatch_challenge.call_args.kwargs
        self.assertEqual(kwargs["battle_config"].pokemon_format, "gen9battletower")

    async def test_start_doubles_challenge(self):
        import db
        mode, cd = self._make_mode()
        await mode.handle_command("testuser", "TestUser", "@battletower", ["double"], "testroom")
        self.assertTrue(cd.dispatch_challenge.called)
        kwargs = cd.dispatch_challenge.call_args.kwargs
        self.assertEqual(kwargs["battle_config"].pokemon_format, "gen9battletowerdoubles")

    async def test_room_number_advances_with_streak(self):
        from fp.modes.battle_tower import _room_for_streak, BATTLES_PER_ROOM
        self.assertEqual(_room_for_streak(0), 1)
        self.assertEqual(_room_for_streak(6), 1)
        self.assertEqual(_room_for_streak(7), 2)
        self.assertEqual(_room_for_streak(13), 2)
        self.assertEqual(_room_for_streak(14), 3)
        self.assertEqual(_room_for_streak(41), 6)
        self.assertEqual(_room_for_streak(42), 7)
        self.assertEqual(_room_for_streak(100), 7)  # caps at MAX_ROOM

    async def test_on_battle_end_win_increments_streak(self):
        import db
        mode, cd = self._make_mode()
        player_id = db.get_or_create_player("towerwintest", "TowerWinTest")

        from config import FoulPlayConfig
        FoulPlayConfig.username = "bot"

        session_data = {
            "player_id": player_id,
            "player_userid": "towerwintest",
            "format_type": "singles",
            "room_num": 1,
            "lobby_room": "testroom",
        }
        await mode.on_battle_end("battle-1", session_data, winner="TowerWinTest")
        rec = db.get_tower_record(player_id, "singles")
        self.assertEqual(rec["current_streak"], 1)

    async def test_on_battle_end_loss_resets_streak(self):
        import db
        mode, cd = self._make_mode()
        player_id = db.get_or_create_player("towerlosstest", "TowerLossTest")
        db.increment_tower_streak(player_id, "singles")
        db.increment_tower_streak(player_id, "singles")

        from config import FoulPlayConfig
        FoulPlayConfig.username = "bot"

        session_data = {
            "player_id": player_id,
            "player_userid": "towerlosstest",
            "format_type": "singles",
            "room_num": 1,
            "lobby_room": "testroom",
        }
        await mode.on_battle_end("battle-2", session_data, winner="bot")
        rec = db.get_tower_record(player_id, "singles")
        self.assertEqual(rec["current_streak"], 0)
        self.assertEqual(rec["max_streak"], 2)  # max preserved

    async def test_towerreset_clears_all(self):
        import db
        mode, cd = self._make_mode()
        player_id = db.get_or_create_player("resettest", "ResetTest")
        db.increment_tower_streak(player_id, "singles")
        await mode.handle_command("resettest", "ResetTest", "@towerreset", [], "testroom")
        rec = db.get_tower_record(player_id, "singles")
        self.assertEqual(rec["current_streak"], 0)
        self.assertEqual(rec["max_streak"], 0)


# ──────────────────────────────────────────────────────────────────────────────
# Battle Factory Mode unit tests
# ──────────────────────────────────────────────────────────────────────────────

class TestBattleFactoryMode(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import db
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.DB_FILE = self.tmp.name
        db.init_db()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _make_mode(self):
        from fp.modes.battle_factory import BattleFactoryMode
        mock_cd = MagicMock()
        mock_cd.send_reply = AsyncMock()
        mock_cd.dispatch_challenge = AsyncMock(return_value=True)
        mock_cd.battle_manager = MagicMock()
        return BattleFactoryMode(mock_cd), mock_cd

    async def test_factory_start_creates_run(self):
        """@factory start should create an in-memory run and trigger draft."""
        mode, cd = self._make_mode()
        import db
        db.get_or_create_player("factuser", "FactUser")

        # Patch _start_draft to not actually call the server
        with patch.object(mode, "_start_draft", new=AsyncMock()) as mock_draft:
            await mode.handle_command("factuser", "FactUser", "@factory", ["start"], "testroom")
            self.assertIn("factuser", mode._runs)
            mock_draft.assert_called_once()

    async def test_factory_draft_requires_active_run(self):
        """@factory draft without a run should return an error reply."""
        mode, cd = self._make_mode()
        await mode.handle_command("nobody", "Nobody", "@factory", ["draft", "1", "2", "3"], "testroom")
        cd.send_reply.assert_called()
        reply = cd.send_reply.call_args.args[2]
        self.assertIn("No active draft", reply)

    async def test_iv_upgrade_logic(self):
        """IV upgrade: each stat +2, capped at 31."""
        from fp.modes.battle_factory import _apply_iv_upgrade, _ivs_from_packed

        # opp set with some IVs in field 8
        opp_parts = ["Opp", "charizard", "choiceband", "blaze",
                     "flamethrower,dragonclaw,earthquake,roost",
                     "adamant", "252/252/4/0/0/0", "M",
                     "31/31/31/31/31/31", "", "50", ""]

        # replaced set IVs = 25/27/29/31/30/0
        replaced_parts = ["Mine", "blastoise", "leftovers", "torrent",
                          "surf,icebeam,rapidspin,toxic",
                          "modest", "252/0/4/252/0/0", "F",
                          "25/27/29/31/30/0", "", "50", ""]

        result = _apply_iv_upgrade(opp_parts, replaced_parts)
        upgraded_ivs = _ivs_from_packed(result[8])

        # Expected: each + 2, capped 31
        self.assertEqual(upgraded_ivs["hp"], 27)   # 25+2
        self.assertEqual(upgraded_ivs["atk"], 29)  # 27+2
        self.assertEqual(upgraded_ivs["def"], 31)  # 29+2
        self.assertEqual(upgraded_ivs["spa"], 31)  # 31+2 → cap 31
        self.assertEqual(upgraded_ivs["spd"], 31)  # 30+2 → cap 31 (wait: 30+2=32 → 31)
        self.assertEqual(upgraded_ivs["spe"], 2)   # 0+2

    async def test_cancel_removes_run(self):
        """@factory cancel should remove run from memory."""
        from fp.modes.battle_factory import BattleFactoryMode, _FactoryRun
        mode, cd = self._make_mode()
        import db
        player_id = db.get_or_create_player("canceltest", "CancelTest")
        mode._runs["canceltest"] = _FactoryRun(player_id, "CancelTest", "testroom")
        self.assertIn("canceltest", mode._runs)
        await mode.cancel_session("canceltest", "CancelTest", "testroom")
        self.assertNotIn("canceltest", mode._runs)

    async def test_on_battle_end_win_increments_streak(self):
        import db
        from config import FoulPlayConfig
        FoulPlayConfig.username = "bot"
        mode, cd = self._make_mode()
        player_id = db.get_or_create_player("factwinner", "FactWinner")
        session_data = {
            "player_id": player_id,
            "player_userid": "factwinner",
            "player_display": "FactWinner",
            "lobby_room": "testroom",
        }
        await mode.on_battle_end("battle-x", session_data, winner="FactWinner")
        rec = db.get_factory_record(player_id)
        self.assertEqual(rec["current_streak"], 1)
        self.assertEqual(rec["total_wins"], 1)

    async def test_on_battle_end_loss_resets_streak(self):
        import db
        from config import FoulPlayConfig
        FoulPlayConfig.username = "bot"
        mode, cd = self._make_mode()
        player_id = db.get_or_create_player("factloser", "FactLoser")
        db.increment_factory_streak(player_id)
        db.increment_factory_streak(player_id)
        session_data = {
            "player_id": player_id,
            "player_userid": "factloser",
            "player_display": "FactLoser",
            "lobby_room": "testroom",
        }
        await mode.on_battle_end("battle-y", session_data, winner="bot")
        rec = db.get_factory_record(player_id)
        self.assertEqual(rec["current_streak"], 0)
        self.assertEqual(rec["max_streak"], 2)

    async def test_start_draft_sends_each_pokemon_before_prompt(self):
        """_start_draft should send each of the 6 pokemon as individual messages before the draft prompt."""
        mode, cd = self._make_mode()
        sample_packed = (
            "Tauros||SpellTag|SheerForce|shadowball,trailblaze,takedown,flamethrower|Naive|6,252,,,,252|M|||50|]"
            "Clefable||MoonStone|CuteCharm|raindance,thunderbolt,dig,waterpulse|Quirky|252,,6,252,,|M|||50|]"
            "Feraligatr||RareBone|Torrent|earthquake,substitute,slash,lowkick|Bashful|6,252,,,,252|F|||50|]"
            "Raichu||MysticWater|Static|grassknot,brickbreak,chargebeam,endeavor|Bashful|6,252,,,,252|M|||50|]"
            "Rotom|RotomFan|PechaBerry|Levitate|thunder,astonish,shadowball,airslash|Hasty|6,,,252,,252|N|||50|]"
            "Noctowl||WeaknessPolicy|TintedLens|tackle,nightshade,gigaimpact,extrasensory|Timid|252,,,252,6,|M|||50|"
        )
        with patch.object(mode, "_generate_team", new=AsyncMock(return_value=sample_packed)):
            await mode.handle_command("draftuser", "DraftUser", "@factory", ["start"], "testroom")
            # 6 Pokémon messages + 1 prompt message = 7 send_reply calls
            self.assertEqual(cd.send_reply.call_count, 7)
            calls = cd.send_reply.call_args_list
            for idx in range(6):
                msg = calls[idx].args[2]
                self.assertTrue(msg.startswith(f"{idx + 1}. "))
            prompt_call = calls[6].args[2]
            self.assertIn("Battle Factory Draft", prompt_call)
            self.assertIn("@factory draft", prompt_call)

    async def test_challenge_dispatcher_send_reply_splits_multiline(self):
        """send_reply without room should send each line as an individual PM."""
        from fp.managers.challenge_dispatcher import ChallengeDispatcher
        mock_ws = MagicMock()
        mock_ws.send_message = AsyncMock()
        cd = ChallengeDispatcher(mock_ws)
        await cd.send_reply("", "User1", "Line 1\nLine 2\nLine 3")
        self.assertEqual(mock_ws.send_message.call_count, 3)
        self.assertEqual(mock_ws.send_message.call_args_list[0].args, ("", ["/pm User1, Line 1"]))
        self.assertEqual(mock_ws.send_message.call_args_list[1].args, ("", ["/pm User1, Line 2"]))
        self.assertEqual(mock_ws.send_message.call_args_list[2].args, ("", ["/pm User1, Line 3"]))

    async def test_factory_status_no_records(self):
        mode, cd = self._make_mode()
        status = await mode.get_player_status("newuser")
        self.assertIn("No records", status)

    async def test_notify_factory_generate_reply_resolves_future(self):
        """notify_factory_generate_reply should resolve the pending Future."""
        mode, cd = self._make_mode()
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        mode._pending_generate["test_key"] = fut
        mode.notify_factory_generate_reply("Bulbasaur||oran|overgrow|tackle||252/4/0/0/0/252||31/31/31/31/31/31||50|]...")
        self.assertTrue(fut.done())
        self.assertIn("Bulbasaur", fut.result())

    async def test_factory_draft_sets_opponent_team_without_split_error(self):
        """@factory draft should parse opp_packed without 'dict object has no attribute split'."""
        import db
        mode, cd = self._make_mode()
        db.get_or_create_player("draftsplituser", "DraftSplitUser")

        # Create active run in draft phase
        sample_draft_pool = [
            ["P1", "Tauros", "SpellTag", "SheerForce", "shadowball", "Naive", "6,252,,,,252", "M", "", "", "50", ""],
            ["P2", "Clefable", "MoonStone", "CuteCharm", "waterpulse", "Quirky", "252,,6,252,,", "M", "", "", "50", ""],
            ["P3", "Feraligatr", "RareBone", "Torrent", "earthquake", "Bashful", "6,252,,,,252", "F", "", "", "50", ""],
            ["P4", "Raichu", "MysticWater", "Static", "grassknot", "Bashful", "6,252,,,,252", "M", "", "", "50", ""],
            ["P5", "Rotom", "PechaBerry", "Levitate", "thunder", "Hasty", "6,,,252,,252", "N", "", "", "50", ""],
            ["P6", "Noctowl", "WeaknessPolicy", "TintedLens", "tackle", "Timid", "252,,,252,6,", "M", "", "", "50", ""],
        ]
        from fp.modes.battle_factory import _FactoryRun
        run = _FactoryRun(
            player_id=1,
            player_display="DraftSplitUser",
            lobby_room="testroom",
        )
        run.draft_pool = sample_draft_pool
        run.phase = "draft"
        mode._runs["draftsplituser"] = run

        opp_packed = (
            "Magnezone||LeppaBerry|MagnetPull|tackle,discharge,spark,zapcannon|Modest|252,,6,252,,|N|||50|]"
            "Rampardos||SharpBeak|MoldBreaker|rocktomb,thief,thrash,trailblaze|Hasty|252,252,6,,,|M|||50|]"
            "Forretress||WideLens|Sturdy|steelbeam,swift,bugbuzz,counter|Careful|252,252,6,,,|F|||50|"
        )
        with patch.object(mode, "_generate_team", new=AsyncMock(return_value=opp_packed)):
            await mode.handle_command("draftsplituser", "DraftSplitUser", "@factory", ["draft", "1", "2", "3"], "testroom")
            self.assertEqual(run.phase, "in_battle")
            self.assertEqual(len(run.opponent_team), 3)
            # Ensure each item in run.opponent_team is a list of parts, not a dict
            self.assertIsInstance(run.opponent_team[0], list)
            species = run.opponent_team[0][1] or run.opponent_team[0][0]
            self.assertEqual(species, "Magnezone")
            cd.dispatch_challenge.assert_called_once()
            call_config = cd.dispatch_challenge.call_args.kwargs["battle_config"]
            self.assertIsNone(call_config.team_packed)
            self.assertIsNone(call_config.team_dict)
            self.assertEqual(call_config.pokemon_format, "gen9battlefactory")

    async def test_factory_concurrent_generation_serialized(self):
        """_generate_team should serialize concurrent requests with _generate_lock."""
        mode, cd = self._make_mode()
        events = []

        async def fake_send_message(room, msgs):
            cmd = msgs[0]
            events.append(f"send:{cmd}")
            await asyncio.sleep(0.01)
            if " 6" in cmd:
                mode.notify_factory_generate_reply("team6")
            elif " 3" in cmd:
                mode.notify_factory_generate_reply("team3")

        cd.ps_websocket_client.send_message = AsyncMock(side_effect=fake_send_message)

        t1 = asyncio.create_task(mode._generate_team(6, "room", "User1"))
        t2 = asyncio.create_task(mode._generate_team(3, "room", "User2"))

        res1, res2 = await asyncio.gather(t1, t2)
        self.assertEqual(res1, "team6")
        self.assertEqual(res2, "team3")
        self.assertEqual(events, ["send:/generatefactoryteam 6", "send:/generatefactoryteam 3"])


# ──────────────────────────────────────────────────────────────────────────────
# CommandDispatcher packed-team detection test
# ──────────────────────────────────────────────────────────────────────────────

class TestCommandDispatcherFactoryInterception(unittest.IsolatedAsyncioTestCase):
    def _make_dispatcher(self):
        from fp.managers.command_dispatcher import CommandDispatcher
        mock_ws = MagicMock()
        mock_ws.send_message = AsyncMock()
        mock_bm = MagicMock()
        mock_bm.has_active_battle_or_challenge = MagicMock(return_value=False)
        mock_cd_inner = MagicMock()
        cd = CommandDispatcher(mock_ws, battle_manager=mock_bm, challenge_dispatcher=mock_cd_inner)
        return cd

    def test_looks_like_packed_team_positive(self):
        from fp.managers.command_dispatcher import CommandDispatcher
        packed = "Bulbasaur||oran|overgrow|tackle||252/4/0/0/0/252||31/31/31/31/31/31||50|]Charmander||..."
        self.assertTrue(CommandDispatcher._looks_like_packed_team(packed))

    def test_looks_like_packed_team_negative_regular_sentence(self):
        from fp.managers.command_dispatcher import CommandDispatcher
        self.assertFalse(CommandDispatcher._looks_like_packed_team("Hello, how are you?"))

    def test_looks_like_packed_team_negative_slash_command(self):
        from fp.managers.command_dispatcher import CommandDispatcher
        self.assertFalse(CommandDispatcher._looks_like_packed_team("/generatefactoryteam 6"))

    async def test_factory_reply_routes_to_mode(self):
        """A bot self-message with a packed team string should call notify_factory_generate_reply."""
        from fp.managers.command_dispatcher import CommandDispatcher
        from config import FoulPlayConfig
        FoulPlayConfig.username = "bot"

        cd = self._make_dispatcher()

        mock_factory_mode = MagicMock()
        mock_factory_mode.mode_id = "battlefactory"
        mock_factory_mode.notify_factory_generate_reply = MagicMock()
        cd.modes_by_id["battlefactory"] = mock_factory_mode

        packed = "Bulbasaur||oranberry|overgrow|tackle,growl,leechseed,sleeppowder||252/4/0/0/0/252||31/31/31/31/31/31||50|]Charmander||..."
        await cd.handle_incoming_text("bot", packed, "testroom")
        mock_factory_mode.notify_factory_generate_reply.assert_called_once_with(packed.strip())

    async def test_factory_reply_routes_with_text_prefix_and_server_sender(self):
        """Showdown sends /text <packed> from server '~' or bot account via PM. It must be intercepted."""
        from config import FoulPlayConfig
        FoulPlayConfig.username = "EvilWoodenPlank"
        cd = self._make_dispatcher()

        mock_factory = MagicMock()
        mock_factory.mode_id = "battlefactory"
        mock_factory.notify_factory_generate_reply = MagicMock()
        cd.modes_by_id["battlefactory"] = mock_factory

        raw_pm = "/text Tauros||SpellTag|SheerForce|shadowball,trailblaze,takedown,flamethrower|Naive|6,252,,,,252|M|||50|]Clefable||MoonStone|CuteCharm|raindance,thunderbolt,dig,waterpulse|Quirky|252,,6,252,,|M|||50|"
        expected = "Tauros||SpellTag|SheerForce|shadowball,trailblaze,takedown,flamethrower|Naive|6,252,,,,252|M|||50|]Clefable||MoonStone|CuteCharm|raindance,thunderbolt,dig,waterpulse|Quirky|252,,6,252,,|M|||50|"

        # Test from server '~'
        await cd.handle_incoming_text("~", raw_pm, "")
        mock_factory.notify_factory_generate_reply.assert_called_once_with(expected)

        # Test from bot '%EvilWoodenPlank'
        mock_factory.notify_factory_generate_reply.reset_mock()
        await cd.handle_incoming_text("%EvilWoodenPlank", raw_pm, "")
        mock_factory.notify_factory_generate_reply.assert_called_once_with(expected)

    def test_pick_move_user_turn1_exact_scenario(self):
        """Verify the exact turn 1 doubles scenario reported by the user."""
        from fp.doubles_battle import DoublesHeuristicBattler
        from data import all_move_json, pokedex
        battler = DoublesHeuristicBattler(all_move_json, pokedex)
        req = {
            "active": [
                {
                    "moves": [
                        {"move": "U-turn", "id": "uturn", "pp": 32, "maxpp": 32, "target": "normal", "disabled": False},
                        {"move": "Double-Edge", "id": "doubleedge", "pp": 24, "maxpp": 24, "target": "normal", "disabled": False},
                        {"move": "Light Screen", "id": "lightscreen", "pp": 48, "maxpp": 48, "target": "allySide", "disabled": False},
                        {"move": "Dazzling Gleam", "id": "dazzlinggleam", "pp": 16, "maxpp": 16, "target": "allAdjacentFoes", "disabled": False}
                    ]
                },
                {
                    "moves": [
                        {"move": "Disarming Voice", "id": "disarmingvoice", "pp": 24, "maxpp": 24, "target": "allAdjacentFoes", "disabled": False},
                        {"move": "Snowscape", "id": "snowscape", "pp": 16, "maxpp": 16, "target": "all", "disabled": False},
                        {"move": "Zen Headbutt", "id": "zenheadbutt", "pp": 24, "maxpp": 24, "target": "normal", "disabled": False},
                        {"move": "Fling", "id": "fling", "pp": 16, "maxpp": 16, "target": "normal", "disabled": False}
                    ]
                }
            ],
            "side": {"pokemon": []},
            "rqid": 2
        }
        opp_species = ["greninja", "farigiraf"]
        choice = battler.pick_move(req, opp_species, rqid=2)
        self.assertEqual(choice, ["/choose move 4, move 1|2"])

    def test_pick_move_cacnea_slowpoke_trailblaze_scenario(self):
        """Cacnea (Trailblaze) + Slowpoke (Tackle) must target positive foe locations (1 or 2), never negative (ally)."""
        from fp.doubles_battle import DoublesHeuristicBattler
        from data import all_move_json, pokedex
        battler = DoublesHeuristicBattler(all_move_json, pokedex)
        req = {
            "active": [
                {
                    "moves": [
                        {"move": "Block", "id": "block", "pp": 8, "maxpp": 8, "target": "normal", "disabled": False},
                        {"move": "Skitter Smack", "id": "skittersmack", "pp": 16, "maxpp": 16, "target": "normal", "disabled": False},
                        {"move": "Destiny Bond", "id": "destinybond", "pp": 8, "maxpp": 8, "target": "self", "disabled": False},
                        {"move": "Trailblaze", "id": "trailblaze", "pp": 32, "maxpp": 32, "target": "normal", "disabled": False},
                    ]
                },
                {
                    "moves": [
                        {"move": "Slack Off", "id": "slackoff", "pp": 8, "maxpp": 8, "target": "self", "disabled": False},
                        {"move": "Imprison", "id": "imprison", "pp": 16, "maxpp": 16, "target": "self", "disabled": False},
                        {"move": "Tackle", "id": "tackle", "pp": 56, "maxpp": 56, "target": "normal", "disabled": False},
                        {"move": "Calm Mind", "id": "calmmind", "pp": 32, "maxpp": 32, "target": "self", "disabled": False},
                    ]
                }
            ],
            "side": {"pokemon": []},
            "rqid": 2
        }
        opp_species = ["pikachu", "raichu"]
        choice = battler.pick_move(req, opp_species, rqid=2)
        self.assertTrue(choice[0].startswith("/choose "))
        self.assertNotIn("-1", choice[0])
        self.assertNotIn("-2", choice[0])
        self.assertIn("move", choice[0])
        self.assertIn("|2", choice[0])

    def test_pick_move_with_forced_switch(self):
        """When forceSwitch is present, battler should choose available reserves."""
        from fp.doubles_battle import DoublesHeuristicBattler
        from data import all_move_json, pokedex
        battler = DoublesHeuristicBattler(all_move_json, pokedex)
        req = {
            "forceSwitch": [True, False],
            "side": {
                "pokemon": [
                    {"ident": "p1: Mon1", "condition": "0 fnt", "active": True},
                    {"ident": "p1: Mon2", "condition": "100/100", "active": True},
                    {"ident": "p1: Mon3", "condition": "100/100", "active": False},
                    {"ident": "p1: Mon4", "condition": "100/100", "active": False},
                ]
            },
            "rqid": 5
        }
        choice = battler.pick_move(req, [None, "farigiraf"], rqid=5)
        self.assertEqual(choice, ["/choose switch 3, pass|5"])

    def test_pick_move_dig_turn2_locked_omits_target(self):
        """Turn 2 of Dig is a locked move with no target field and trapped=True; must emit move 1 without target."""
        from fp.doubles_battle import DoublesHeuristicBattler
        from data import all_move_json, pokedex
        battler = DoublesHeuristicBattler(all_move_json, pokedex)
        req = {
            "active": [
                {
                    "moves": [{"move": "Dig", "id": "dig"}],
                    "trapped": True,
                },
                {
                    "moves": [
                        {"move": "Tackle", "id": "tackle", "pp": 35, "maxpp": 35, "target": "normal", "disabled": False},
                    ]
                }
            ],
            "side": {"pokemon": []},
            "rqid": 6
        }
        opp_species = ["pikachu", "raichu"]
        choice = battler.pick_move(req, opp_species, rqid=6)
        # Slot 0 (Dig turn 2) must be "move 1" without target!
        # Slot 1 (Tackle) should be "move 1 1" with target!
        self.assertEqual(choice, ["/choose move 1, move 1 1|6"])


if __name__ == "__main__":
    unittest.main()
