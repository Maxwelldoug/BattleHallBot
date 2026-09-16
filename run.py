import asyncio
import json
import logging
import traceback
from copy import deepcopy

from config import FoulPlayConfig, init_logging, BotModes

from teams import load_team, TeamListIterator
from fp.run_battle import pokemon_battle
from fp.websocket_client import PSWebsocketClient

from data import all_move_json
from data import pokedex
from data.mods.apply_mods import apply_mods

logger = logging.getLogger(__name__)

import math
from fp.helpers import normalize_name

def calculate_challenge_level(player_level: int, current_type_wins: int, other_types_with_wins: int) -> int:
    level_player = float(player_level)
    sqrt_lp = math.sqrt(level_player)
    level_base = level_player - (3.0 * sqrt_lp)
    increment = sqrt_lp / 5.0
    rank = current_type_wins + 1
    val = level_base + (other_types_with_wins / 2.0) + ((rank - 1) * increment)
    calculated = math.ceil(val)
    challenge_level = min(int(level_player), int(calculated))
    return max(1, min(100, challenge_level))

def extract_opponent_userid(msg, bot_username):
    user_name = normalize_name(bot_username)
    lines = msg.split("\n")
    for line in lines:
        if line.startswith("|title|"):
            title_text = line.replace("|title|", "").strip()
            parts = [p.strip() for p in title_text.split("vs.")]
            if len(parts) == 2:
                p1_id = normalize_name(parts[0])
                p2_id = normalize_name(parts[1])
                if p1_id == user_name:
                    return p2_id
                elif p2_id == user_name:
                    return p1_id
        elif line.startswith("|player|"):
            parts = line.split("|")
            if len(parts) >= 4:
                p_id = normalize_name(parts[3])
                if p_id != user_name:
                    return p_id
    return None



def check_dictionaries_are_unmodified(original_pokedex, original_move_json):
    # The bot should not modify the data dictionaries
    # This is a "just-in-case" check to make sure and will stop the bot if it mutates either of them
    if original_move_json != all_move_json:
        logger.critical(
            "Move JSON changed!\nDumping modified version to `modified_moves.json`"
        )
        with open("modified_moves.json", "w") as f:
            json.dump(all_move_json, f, indent=4)
        exit(1)
    else:
        logger.debug("Move JSON unmodified!")

    if original_pokedex != pokedex:
        logger.critical(
            "Pokedex JSON changed!\nDumping modified version to `modified_pokedex.json`"
        )
        with open("modified_pokedex.json", "w") as f:
            json.dump(pokedex, f, indent=4)
        exit(1)
    else:
        logger.debug("Pokedex JSON unmodified!")


async def run_foul_play():
    FoulPlayConfig.configure()
    init_logging(FoulPlayConfig.log_level, FoulPlayConfig.log_to_file)
    apply_mods(FoulPlayConfig.pokemon_format)

    original_pokedex = deepcopy(pokedex)
    original_move_json = deepcopy(all_move_json)

    ps_websocket_client = await PSWebsocketClient.create(
        FoulPlayConfig.username, FoulPlayConfig.password, FoulPlayConfig.websocket_uri
    )

    FoulPlayConfig.user_id = await ps_websocket_client.login()

    if FoulPlayConfig.avatar is not None:
        await ps_websocket_client.avatar(FoulPlayConfig.avatar)

    team_iterator = (
        None
        if FoulPlayConfig.team_list is None
        else TeamListIterator(FoulPlayConfig.team_list)
    )
    battles_run = 0
    wins = 0
    losses = 0
    team_file_name = "None"
    team_dict = None
    if FoulPlayConfig.bot_mode == BotModes.battlehall:
        import db
        from fp.search.compute_pool import MCTSComputePool
        from fp.managers.challenge_dispatcher import ChallengeDispatcher
        from fp.managers.battle_manager import ActiveBattleManager
        from fp.managers.command_dispatcher import CommandDispatcher
        from fp.modes.battle_hall import BattleHallMode
        from fp.modes.battle_tower import BattleTowerMode
        from fp.modes.battle_factory import BattleFactoryMode

        db.init_db()
        lobby_room = FoulPlayConfig.room_name or "lobby"
        await ps_websocket_client.join_room(lobby_room)
        ps_websocket_client.start_router()

        compute_pool = MCTSComputePool.get_instance()
        challenge_dispatcher = ChallengeDispatcher(ps_websocket_client)
        battle_manager = ActiveBattleManager(
            ps_websocket_client,
            max_concurrent_battles=FoulPlayConfig.max_concurrent_battles,
        )
        challenge_dispatcher.set_battle_manager(battle_manager)
        battle_manager.set_challenge_dispatcher(challenge_dispatcher)

        command_dispatcher = CommandDispatcher(
            ps_websocket_client,
            battle_manager=battle_manager,
            challenge_dispatcher=challenge_dispatcher,
        )
        battle_hall_mode = BattleHallMode(challenge_dispatcher)
        battle_tower_mode = BattleTowerMode(challenge_dispatcher)
        battle_factory_mode = BattleFactoryMode(challenge_dispatcher)
        command_dispatcher.register_mode(battle_hall_mode)
        command_dispatcher.register_mode(battle_tower_mode)
        command_dispatcher.register_mode(battle_factory_mode)

        battle_manager.start_orchestrator()
        command_dispatcher.start_listeners(lobby_room)

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await command_dispatcher.stop_listeners()
            await battle_manager.stop_orchestrator()
            compute_pool.shutdown()
            await ps_websocket_client.close()

    while True:
        if FoulPlayConfig.requires_team():
            team_name = (
                team_iterator.get_next_team()
                if team_iterator is not None
                else FoulPlayConfig.team_name
            )
            team_packed, team_dict, team_file_name = load_team(team_name)
            await ps_websocket_client.update_team(team_packed)
        else:
            await ps_websocket_client.update_team("None")

        if FoulPlayConfig.bot_mode == BotModes.challenge_user:
            await ps_websocket_client.challenge_user(
                FoulPlayConfig.user_to_challenge,
                FoulPlayConfig.pokemon_format,
            )
        elif FoulPlayConfig.bot_mode == BotModes.accept_challenge:
            await ps_websocket_client.accept_challenge(
                FoulPlayConfig.pokemon_format, FoulPlayConfig.room_name
            )
        elif FoulPlayConfig.bot_mode == BotModes.search_ladder:
            await ps_websocket_client.search_for_match(FoulPlayConfig.pokemon_format)
        else:
            raise ValueError("Invalid Bot Mode: {}".format(FoulPlayConfig.bot_mode))

        winner = await pokemon_battle(
            ps_websocket_client, FoulPlayConfig.pokemon_format, team_dict
        )
        if winner == FoulPlayConfig.username:
            wins += 1
            logger.info("Won with team: {}".format(team_file_name))
        else:
            losses += 1
            logger.info("Lost with team: {}".format(team_file_name))

        logger.info("W: {}\tL: {}".format(wins, losses))
        check_dictionaries_are_unmodified(original_pokedex, original_move_json)

        battles_run += 1
        if battles_run >= FoulPlayConfig.run_count:
            break
    await ps_websocket_client.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_foul_play())
    except Exception:
        logger.error(traceback.format_exc())
        raise
