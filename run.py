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
        import math
        from fp.helpers import normalize_name
        import time

        db.init_db()
        lobby_room = FoulPlayConfig.room_name or "lobby"
        await ps_websocket_client.join_room(lobby_room)
        ps_websocket_client.start_router()

        active_battle = None
        state_lock = asyncio.Lock()

        genesect_packed = "Genesect|||Download|uturn|||||||||||"
        genesect_dict = [{
            "name": "Genesect",
            "species": "genesect",
            "level": 100,
            "tera_type": "steel",
            "gender": "",
            "item": "",
            "ability": "download",
            "moves": ["uturn"],
            "shiny": "",
            "nature": "serious",
            "ivs": {"hp": "31", "atk": "31", "def": "31", "spa": "31", "spd": "31", "spe": "31"},
            "evs": {"hp": "0", "atk": "0", "def": "0", "spa": "0", "spd": "0", "spe": "0"}
        }]


        async def send_reply(room, player_display, msg_text):
            if room == "":
                await ps_websocket_client.send_message("", ["/pm {}, {}".format(player_display, msg_text)])
            else:
                await ps_websocket_client.send_message(room, [msg_text])

        async def challenge_timeout_monitor(player_userid, challenge_sent_time):
            await asyncio.sleep(60)
            async with state_lock:
                nonlocal active_battle
                if (
                    active_battle 
                    and active_battle["player_userid"] == player_userid 
                    and active_battle["challenge_sent_time"] == challenge_sent_time
                    and "battle_started" not in active_battle
                ):
                    await ps_websocket_client.send_message("", ["/cancel {}".format(active_battle["player_display"])])
                    await send_reply(active_battle["room_context"], active_battle["player_display"], "Challenge timed out.")
                    active_battle = None


        async def battle_orchestration_loop():
            nonlocal active_battle
            while True:
                battle_tag, msg = await ps_websocket_client.pending_battles_queue.get()
                
                # 1. Enforce strict Battle Hall format checking
                if not battle_tag.startswith("battle-gen9battlehall-"):
                    logger.warning("Ignoring unexpected/non-BattleHall room: {}".format(battle_tag))
                    await ps_websocket_client.send_message(battle_tag, ["Sorry, I am currently busy or this battle was not requested."])
                    await ps_websocket_client.leave_battle(battle_tag)
                    continue

                opponent_userid = extract_opponent_userid(msg, FoulPlayConfig.username)

                # 2. Delayed opponent name resolution (up to 10 seconds)
                # Only push back frames we actually consumed from the room queue.
                messages_read = []
                while opponent_userid is None:
                    try:
                        m = await asyncio.wait_for(
                            ps_websocket_client.receive_message(room=battle_tag),
                            timeout=10.0
                        )
                        messages_read.append(m)
                        opponent_userid = extract_opponent_userid(m, FoulPlayConfig.username)
                    except asyncio.TimeoutError:
                        logger.warning("Timeout waiting for opponent info in room {}".format(battle_tag))
                        break

                current_b = None
                async with state_lock:
                    if (
                        active_battle is not None 
                        and opponent_userid == active_battle["player_userid"]
                    ):
                        active_battle["battle_started"] = True
                        current_b = active_battle.copy()
                
                if current_b is None:
                    logger.warning("Ignoring unexpected/unsolicited battle {} against {}".format(battle_tag, opponent_userid))
                    await ps_websocket_client.send_message(battle_tag, ["Sorry, I am currently busy or this battle was not requested."])
                    await ps_websocket_client.leave_battle(battle_tag)
                    continue

                # Legit challenge! Push back all read messages to preserve sequence for battle hooks
                for m in messages_read:
                    ps_websocket_client.push_back_message(m, room=battle_tag)

                logger.info("Starting Battle Hall match in room {} against {}".format(battle_tag, current_b["player_display"]))
                
                try:
                    winner = await pokemon_battle(ps_websocket_client, "gen9battlehall", genesect_dict, battle_tag=battle_tag)
                    winner_userid = normalize_name(winner) if winner else ""
                    opponent_userid = current_b["player_userid"]
                    
                    if winner_userid == opponent_userid:
                        db.increment_player_wins(current_b["player_id"], current_b["type_id"])
                        new_wins = db.get_player_wins(current_b["player_id"], current_b["type_id"])
                        await send_reply(
                            current_b["room_context"], 
                            current_b["player_display"], 
                            "Congratulations! You won the {} battle. Your total wins for this type is now {}/10.".format(
                                current_b['printed_type'], new_wins
                            )
                        )
                    else:
                        db.reset_player_all_wins(current_b["player_id"])
                        await send_reply(
                            current_b["room_context"], 
                            current_b["player_display"], 
                            "You lost the battle! All of your Battle Hall wins have been reset to 0."
                        )
                except Exception as e:
                    logger.exception("Error during battle: {}".format(e))
                    await send_reply(
                        current_b["room_context"], 
                        current_b["player_display"], 
                        "An error occurred during the battle. Win/loss was not updated."
                    )
                finally:
                    async with state_lock:
                        active_battle = None

        orchestrator_task = asyncio.create_task(battle_orchestration_loop())

        async def handle_incoming_text(username, text, room_context):
            nonlocal active_battle
            sender_userid = normalize_name(username)
            if sender_userid == normalize_name(FoulPlayConfig.username):
                return

            cleaned_text = text.strip()
            if cleaned_text.startswith("!battlehallreset"):
                player_id = db.get_or_create_player(sender_userid, username)
                db.reset_player_all_wins(player_id)
                await send_reply(room_context, username, "All of your Battle Hall wins have been reset to 0.")
                return

            elif cleaned_text.startswith("!battlehall"):
                parts = cleaned_text.split()
                if len(parts) != 3:
                    await send_reply(room_context, username, "Usage: !battlehall [type] [level] (e.g., !battlehall fire 50)")
                    return

                type_input = parts[1]
                level_input = parts[2]

                canonical_type = normalize_name(type_input)
                type_row = db.get_type_by_canonical_name(canonical_type)
                if not type_row:
                    await send_reply(
                        room_context, 
                        username, 
                        "Invalid type: {}. Valid types: Normal, Fire, Water, Grass, Electric, Ice, Fighting, Poison, Ground, Flying, Psychic, Bug, Rock, Ghost, Dragon, Steel, Dark, Fairy.".format(type_input)
                    )
                    return

                type_id, printed_type = type_row

                try:
                    player_level = int(level_input)
                    if not (1 <= player_level <= 100):
                        raise ValueError()
                except ValueError:
                    await send_reply(room_context, username, "Invalid level: {}. Level must be an integer between 1 and 100.".format(level_input))
                    return

                async with state_lock:
                    if active_battle is not None:
                        await send_reply(room_context, username, "The bot is currently busy with a challenge. Please wait.")
                        return

                    player_id = db.get_or_create_player(sender_userid, username)
                    wins = db.get_player_wins(player_id, type_id)
                    if wins >= 10:
                        await send_reply(room_context, username, "You have already beaten {} 10 times! Choose another type or reset with !battlehallreset.".format(printed_type))
                        return

                    other_types_with_wins = db.get_other_types_with_wins_count(player_id, type_id)
                    challenge_level = calculate_challenge_level(player_level, wins, other_types_with_wins)

                    sent_time = time.time()
                    active_battle = {
                        "player_userid": sender_userid,
                        "player_display": username,
                        "canonical_type": canonical_type,
                        "printed_type": printed_type,
                        "type_id": type_id,
                        "player_id": player_id,
                        "player_level": player_level,
                        "challenge_level": challenge_level,
                        "room_context": room_context,
                        "challenge_sent_time": sent_time
                    }

                await ps_websocket_client.send_message("", ["/battlehalllevel {}".format(challenge_level)])
                await ps_websocket_client.send_message("", ["/battlehalltype {}".format(canonical_type)])
                await ps_websocket_client.update_team(genesect_packed)
                await ps_websocket_client.send_message("", ["/challenge {},gen9battlehall".format(username)])
                await send_reply(room_context, username, "Challenged to a {} Battle (Rank {}, Level {})!".format(printed_type, wins + 1, challenge_level))

                asyncio.create_task(challenge_timeout_monitor(sender_userid, sent_time))

            elif "rejected the challenge" in cleaned_text or "cancelled the challenge" in cleaned_text:
                async with state_lock:
                    if active_battle and active_battle["player_display"].lower() in cleaned_text.lower():
                        await send_reply(active_battle["room_context"], active_battle["player_display"], "Challenge was rejected.")
                        active_battle = None

        async def lobby_listener():
            while True:
                msg = await ps_websocket_client.receive_message(room=lobby_room)
                parts = msg.split("|")
                if len(parts) >= 4 and parts[1] == "c":
                    username = parts[2]
                    text = "|".join(parts[3:])
                    await handle_incoming_text(username, text, lobby_room)
                elif len(parts) >= 5 and parts[1] == "c:":
                    username = parts[3]
                    text = "|".join(parts[4:])
                    await handle_incoming_text(username, text, lobby_room)

        async def pm_listener():
            while True:
                msg = await ps_websocket_client.receive_message(room="")
                parts = msg.split("|")
                if len(parts) >= 5 and parts[1] == "pm":
                    sender = parts[2].strip()
                    text = "|".join(parts[4:])
                    await handle_incoming_text(sender, text, "")

        try:
            await asyncio.gather(
                lobby_listener(),
                pm_listener()
            )
        finally:
            orchestrator_task.cancel()
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
