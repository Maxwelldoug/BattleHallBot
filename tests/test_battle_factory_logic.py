"""
Unit tests for Battle Factory battle logic fixes:
- _fallback_move behavior on forced switch / faint
- Battle.team_size property across facility modes
- Sampling unrevealed Pokémon respecting team_size
- Fallback initialization of RandomBattleTeamDatasets and Battle Factory sets
"""
import unittest

from constants import BattleType
from fp.battle import Battle, Pokemon, Move
from fp.search.compute_pool import MCTSComputePool
from fp.search.random_battles import (
    populate_randombattle_unrevealed_pkmn,
    sample_randombattle_pokemon,
    get_all_remaining_sets_for_revealed_pkmn,
)
from data.pkmn_sets import RandomBattleTeamDatasets, TeamDatasets


class TestBattleFactoryBattleLogic(unittest.IsolatedAsyncioTestCase):
    def test_team_size_property(self):
        """Battle.team_size should reflect the correct max size for each facility."""
        b_factory = Battle("test-factory")
        b_factory.pokemon_format = "gen9battlefactory"
        self.assertEqual(b_factory.team_size, 3)

        b_hall = Battle("test-hall")
        b_hall.pokemon_format = "gen9battlehall"
        self.assertEqual(b_hall.team_size, 1)

        b_tower_singles = Battle("test-tower")
        b_tower_singles.pokemon_format = "gen9battletower"
        self.assertEqual(b_tower_singles.team_size, 3)

        b_tower_doubles = Battle("test-tower-doubles")
        b_tower_doubles.pokemon_format = "gen9battletowerdoubles"
        self.assertEqual(b_tower_doubles.team_size, 4)

        b_ou = Battle("test-ou")
        b_ou.pokemon_format = "gen9ou"
        b_ou.user.active = Pokemon("garchomp", 100)
        b_ou.user.reserve = [Pokemon("gliscor", 100) for _ in range(5)]
        self.assertEqual(b_ou.team_size, 6)

    def test_fallback_move_on_force_switch(self):
        """When force_switch is True, _fallback_move must return 'switch <alive_reserve>'."""
        pool = MCTSComputePool.get_instance()
        battle = Battle("test-fallback")
        battle.pokemon_format = "gen9battlefactory"
        battle.force_switch = True

        p1 = Pokemon("probopass", 50)
        p1.hp = 0
        p1.fainted = True
        battle.user.active = p1

        p2 = Pokemon("flygon", 50)
        p2.hp = 100
        p2.fainted = False

        p3 = Pokemon("charizard", 50)
        p3.hp = 100
        p3.fainted = False

        battle.user.reserve = [p2, p3]

        move = pool._fallback_move(battle)
        self.assertEqual(move, "switch flygon")

    def test_fallback_move_skips_fainted_reserve(self):
        """When the first reserve is fainted, _fallback_move picks the next alive reserve."""
        pool = MCTSComputePool.get_instance()
        battle = Battle("test-fallback-fainted")
        battle.force_switch = True

        p1 = Pokemon("probopass", 50)
        p1.hp = 0
        battle.user.active = p1

        p2 = Pokemon("flygon", 50)
        p2.hp = 0
        p2.fainted = True

        p3 = Pokemon("charizard", 50)
        p3.hp = 100
        p3.fainted = False

        battle.user.reserve = [p2, p3]

        move = pool._fallback_move(battle)
        self.assertEqual(move, "switch charizard")

    def test_fallback_move_picks_usable_move_when_active_alive(self):
        """When active is alive and not force switch, pick first usable move with PP."""
        pool = MCTSComputePool.get_instance()
        battle = Battle("test-usable-move")
        battle.force_switch = False

        active = Pokemon("probopass", 50)
        m1 = Move("thunderwave")
        m1.current_pp = 0
        m2 = Move("stealthrock")
        m2.current_pp = 10
        m2.disabled = False
        active.moves = [m1, m2]

        battle.user.active = active
        move = pool._fallback_move(battle)
        self.assertEqual(move, "stealthrock")

    def test_populate_randombattle_unrevealed_respects_team_size(self):
        """populate_randombattle_unrevealed_pkmn should only sample up to battle.team_size."""
        RandomBattleTeamDatasets.initialize("gen9")
        battle = Battle("test-unrevealed-size")
        battle.pokemon_format = "gen9battlefactory"
        battle.battle_type = BattleType.BATTLE_FACTORY

        opp = Pokemon("flygon", 50)
        battle.opponent.active = opp
        battle.opponent.reserve = []

        # 1 active revealed, team_size is 3 -> exactly 2 should be sampled
        populate_randombattle_unrevealed_pkmn(battle)
        total_opp_pkmn = len(battle.opponent.reserve) + 1
        self.assertEqual(total_opp_pkmn, 3)

        # Calling again when 3 are revealed does not add more
        populate_randombattle_unrevealed_pkmn(battle)
        self.assertEqual(len(battle.opponent.reserve) + 1, 3)

    def test_battle_factory_team_datasets_and_sets_fallback(self):
        """TeamDatasets loads factory-sets.json and provides sets for Battle Factory pokemon."""
        TeamDatasets.initialize("gen9battlefactory", {"probopass"})
        RandomBattleTeamDatasets.initialize("gen9")

        battle = Battle("test-factory-sets")
        battle.pokemon_format = "gen9battlefactory"
        battle.battle_type = BattleType.BATTLE_FACTORY

        # Flygon is in factory-sets.json
        opp_flygon = Pokemon("flygon", 50)
        battle.opponent.active = opp_flygon
        battle.opponent.reserve = []

        sets = get_all_remaining_sets_for_revealed_pkmn(battle)
        self.assertIn("flygon", sets)
        self.assertGreater(len(sets["flygon"]), 0)

        # Probopass is not in factory-sets.json, should fallback to RandomBattleTeamDatasets
        opp_probo = Pokemon("probopass", 50)
        battle.opponent.active = opp_probo
        sets_probo = get_all_remaining_sets_for_revealed_pkmn(battle)
        self.assertIn("probopass", sets_probo)
        self.assertGreater(len(sets_probo["probopass"]), 0)

    def test_format_mon_summary_unnamed_species(self):
        """When Showdown packs a set with no nickname, species is in field 0 and field 1 is empty."""
        from fp.modes.battle_factory import _format_mon_summary
        parts = ["Flygon", "", "PomegBerry", "Levitate", "earthquake,scaleshot", "Jolly", "0,252,4,0,0,252"]
        species, item = _format_mon_summary(parts)
        self.assertEqual(species, "Flygon")
        self.assertEqual(item, "PomegBerry")

    def test_format_mon_summary_with_nickname(self):
        """When a Pokémon has a nickname, species is in field 1."""
        from fp.modes.battle_factory import _format_mon_summary
        parts = ["Dragon", "Flygon", "PasshoBerry", "Levitate", "earthquake,scaleshot", "Jolly", "0,252,4,0,0,252"]
        species, item = _format_mon_summary(parts)
        self.assertEqual(species, "Flygon")
        self.assertEqual(item, "PasshoBerry")

    async def test_present_swap_offer_includes_species(self):
        """_present_swap_offer should format each mon with its species name and item."""
        from fp.modes.battle_factory import BattleFactoryMode, _FactoryRun
        from unittest.mock import AsyncMock, MagicMock

        mock_cd = MagicMock()
        mock_cd.send_reply = AsyncMock()
        mode = BattleFactoryMode(mock_cd)

        run = _FactoryRun(1, "TestUser", "testroom")
        run.opponent_team = [
            ["Flygon", "", "PomegBerry", "Levitate"],
            ["Giratina", "", "GriseousCore", "Pressure"],
            ["Slowbro", "", "PasshoBerry", "Regenerator"],
        ]
        run.player_team = [
            ["Sandslash", "", "IceStone", "SnowCloak"],
            ["Ceruledge", "", "MaliciousArmor", "FlashFire"],
            ["Cloyster", "", "KingsRock", "SkillLink"],
        ]

        await mode._present_swap_offer(run, "TestUser", "testroom")

        messages = [call.args[2] for call in mock_cd.send_reply.call_args_list]
        self.assertTrue(any("1. Flygon  @ PomegBerry" in m for m in messages))
        self.assertTrue(any("2. Giratina  @ GriseousCore" in m for m in messages))
        self.assertTrue(any("3. Slowbro  @ PasshoBerry" in m for m in messages))
        self.assertTrue(any("1. Sandslash  @ IceStone" in m for m in messages))
        self.assertTrue(any("2. Ceruledge  @ MaliciousArmor" in m for m in messages))
        self.assertTrue(any("3. Cloyster  @ KingsRock" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
