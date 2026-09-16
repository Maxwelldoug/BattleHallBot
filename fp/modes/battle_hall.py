import math
import logging
from typing import List, Optional, Dict, Any

import db
from fp.helpers import normalize_name
from fp.modes.base import BaseGameMode, BattleConfiguration
from teams.team_converter import json_to_packed

logger = logging.getLogger(__name__)

GENESECT_DICT = [
    {
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
        "ivs": {
            "hp": "31",
            "atk": "31",
            "def": "31",
            "spa": "31",
            "spd": "31",
            "spe": "31",
        },
        "evs": {
            "hp": "0",
            "atk": "252",
            "def": "4",
            "spa": "0",
            "spd": "0",
            "spe": "252",
        },
        "happiness": 255,
    }
]
GENESECT_PACKED = json_to_packed(GENESECT_DICT)


def calculate_challenge_level(
    player_level: int, current_type_wins: int, other_types_with_wins: int
) -> int:
    level_player = float(player_level)
    sqrt_lp = math.sqrt(level_player)
    level_base = level_player - (3.0 * sqrt_lp)
    increment = sqrt_lp / 5.0
    rank = current_type_wins + 1
    val = level_base + (other_types_with_wins / 2.0) + ((rank - 1) * increment)
    calculated = math.ceil(val)
    challenge_level = min(int(level_player), int(calculated))
    return max(1, min(100, challenge_level))


class BattleHallMode(BaseGameMode):
    def __init__(self, challenge_dispatcher: Any):
        self.challenge_dispatcher = challenge_dispatcher

    @property
    def mode_id(self) -> str:
        return "battlehall"

    @property
    def command_prefixes(self) -> List[str]:
        return [
            "@battlehall",
            "@bh",
            "@battlehallreset",
            "@battlehallstatus",
            "@battlehallcancel",
        ]

    async def send_reply(self, room: str, player_display: str, msg_text: str):
        await self.challenge_dispatcher.send_reply(
            room, player_display, msg_text
        )

    async def get_player_status(self, player_userid: str) -> str:
        player = db.get_player_by_userid(player_userid)
        if not player:
            return "Battle Hall: No recorded battles yet. Start with @battlehall [type] [level]."

        player_id = player["id"]
        total_wins = db.get_player_total_wins(player_id)
        session = db.get_session(player_id, mode=self.mode_id)

        if session:
            current_type_wins = db.get_player_wins(
                player_id, session["type_id"]
            )
            return (
                "Battle Hall: Active run on {} (Rank {}/10, Level {}). "
                "Total career streak: {}/180 wins.".format(
                    session["printed_type"],
                    current_type_wins + 1,
                    session["challenge_level"],
                    total_wins,
                )
            )
        else:
            return "Battle Hall: Career streak: {}/180 wins. No active run. Start one with @battlehall [type] [level].".format(
                total_wins
            )

    async def cancel_session(
        self, player_userid: str, player_display: str, room_context: str
    ) -> str:
        player = db.get_player_by_userid(player_userid)
        if player:
            db.clear_session(player["id"], mode=self.mode_id)
        msg = "Your Battle Hall session was cleared."
        await self.send_reply(room_context, player_display, msg)
        return msg

    async def prepare_battle(
        self, player_userid: str
    ) -> Optional[BattleConfiguration]:
        player = db.get_player_by_userid(player_userid)
        if not player:
            return None
        session = db.get_session(player["id"], mode=self.mode_id)
        if not session:
            return None

        return BattleConfiguration(
            pokemon_format="gen9battlehall",
            team_dict=GENESECT_DICT,
            team_packed=GENESECT_PACKED,
            setup_commands=[
                "/battlehalllevel {}".format(session["challenge_level"]),
                "/battlehalltype {}".format(session["canonical_type"]),
            ],
            extra_info={
                "player_id": player["id"],
                "type_id": session["type_id"],
                "printed_type": session["printed_type"],
                "canonical_type": session["canonical_type"],
                "player_level": session["player_level"],
                "challenge_level": session["challenge_level"],
                "room_context": session["room_context"],
            },
        )

    async def handle_command(
        self,
        player_userid: str,
        player_display: str,
        command: str,
        args: List[str],
        room_context: str,
    ):
        cmd = command.lower()

        # 1. Reset command
        if cmd == "@battlehallreset":
            player_id = db.get_or_create_player(player_userid, player_display)
            db.reset_player_all_wins(player_id)
            db.clear_session(player_id, mode=self.mode_id)
            await self.send_reply(
                room_context,
                player_display,
                "All of your Battle Hall wins have been reset to 0.",
            )
            return

        # 2. Status command
        if cmd == "@battlehallstatus" or (
            cmd in ("@battlehall", "@bh")
            and args
            and args[0].lower() == "status"
        ):
            status_text = await self.get_player_status(player_userid)
            await self.send_reply(room_context, player_display, status_text)
            return

        # 3. Cancel command
        if cmd == "@battlehallcancel" or (
            cmd in ("@battlehall", "@bh")
            and args
            and args[0].lower() == "cancel"
        ):
            # Check manager cancellation first
            cancelled = False
            bm = getattr(self.challenge_dispatcher, "battle_manager", None)
            if bm:
                if bm.remove_from_wait_queue(player_userid):
                    cancelled = True
                if await bm.cancel_pending_challenge(player_userid):
                    cancelled = True

            await self.cancel_session(player_userid, player_display, room_context)
            return

        # 4. Resume command: @battlehall without arguments resumes active session
        if cmd in ("@battlehall", "@bh") and len(args) == 0:
            player = db.get_player_by_userid(player_userid)
            session = (
                db.get_session(player["id"], mode=self.mode_id)
                if player
                else None
            )
            if not session:
                await self.send_reply(
                    room_context,
                    player_display,
                    "Usage: @battlehall [type] [level] (e.g., @battlehall fire 50)",
                )
                return

            # Resume existing session
            player_id = player["id"]
            type_id = session["type_id"]
            printed_type = session["printed_type"]
            canonical_type = session["canonical_type"]
            player_level = session["player_level"]
            wins = db.get_player_wins(player_id, type_id)

            if wins >= 10:
                await self.send_reply(
                    room_context,
                    player_display,
                    "You have already beaten {} 10 times! Choose another type or reset with @battlehallreset.".format(
                        printed_type
                    ),
                )
                return

            other_types = db.get_other_types_with_wins_count(
                player_id, type_id
            )
            challenge_level = calculate_challenge_level(
                player_level, wins, other_types
            )
            db.save_session(
                player_id,
                self.mode_id,
                type_id,
                player_level,
                challenge_level,
                room_context,
                status="active",
            )

            battle_config = BattleConfiguration(
                pokemon_format="gen9battlehall",
                team_dict=GENESECT_DICT,
                team_packed=GENESECT_PACKED,
                setup_commands=[
                    "/battlehalllevel {}".format(challenge_level),
                    "/battlehalltype {}".format(canonical_type),
                ],
                extra_info={
                    "player_id": player_id,
                    "type_id": type_id,
                    "printed_type": printed_type,
                    "canonical_type": canonical_type,
                    "player_level": player_level,
                    "challenge_level": challenge_level,
                    "room_context": room_context,
                },
            )

            dispatched = await self.challenge_dispatcher.dispatch_challenge(
                player_userid=player_userid,
                player_display=player_display,
                mode=self,
                battle_config=battle_config,
                room_context=room_context,
                session_data=battle_config.extra_info,
            )
            if dispatched:
                await self.send_reply(
                    room_context,
                    player_display,
                    "Resuming Battle Hall! Challenged to a {} Battle (Rank {}, Level {})!".format(
                        printed_type, wins + 1, challenge_level
                    ),
                )
            return

        # 5. New challenge: @battlehall [type] [level]
        if len(args) != 2:
            await self.send_reply(
                room_context,
                player_display,
                "Usage: @battlehall [type] [level] (e.g., @battlehall fire 50)",
            )
            return

        type_input = args[0]
        level_input = args[1]

        canonical_type = normalize_name(type_input)
        type_row = db.get_type_by_canonical_name(canonical_type)
        if not type_row:
            await self.send_reply(
                room_context,
                player_display,
                "Invalid type: {}. Valid types: Normal, Fire, Water, Grass, Electric, Ice, Fighting, Poison, Ground, Flying, Psychic, Bug, Rock, Ghost, Dragon, Steel, Dark, Fairy.".format(
                    type_input
                ),
            )
            return

        type_id, printed_type = type_row

        try:
            player_level = int(level_input)
            if not (1 <= player_level <= 100):
                raise ValueError()
        except ValueError:
            await self.send_reply(
                room_context,
                player_display,
                "Invalid level: {}. Level must be an integer between 1 and 100.".format(
                    level_input
                ),
            )
            return

        player_id = db.get_or_create_player(player_userid, player_display)
        wins = db.get_player_wins(player_id, type_id)
        if wins >= 10:
            await self.send_reply(
                room_context,
                player_display,
                "You have already beaten {} 10 times! Choose another type or reset with @battlehallreset.".format(
                    printed_type
                ),
            )
            return

        other_types_with_wins = db.get_other_types_with_wins_count(
            player_id, type_id
        )
        challenge_level = calculate_challenge_level(
            player_level, wins, other_types_with_wins
        )

        db.save_session(
            player_id=player_id,
            mode=self.mode_id,
            type_id=type_id,
            player_level=player_level,
            challenge_level=challenge_level,
            room_context=room_context,
            status="active",
        )

        session_data = {
            "player_id": player_id,
            "type_id": type_id,
            "printed_type": printed_type,
            "canonical_type": canonical_type,
            "player_level": player_level,
            "challenge_level": challenge_level,
            "room_context": room_context,
        }

        battle_config = BattleConfiguration(
            pokemon_format="gen9battlehall",
            team_dict=GENESECT_DICT,
            team_packed=GENESECT_PACKED,
            setup_commands=[
                "/battlehalllevel {}".format(challenge_level),
                "/battlehalltype {}".format(canonical_type),
            ],
            extra_info=session_data,
        )

        dispatched = await self.challenge_dispatcher.dispatch_challenge(
            player_userid=player_userid,
            player_display=player_display,
            mode=self,
            battle_config=battle_config,
            room_context=room_context,
            session_data=session_data,
        )
        if dispatched:
            await self.send_reply(
                room_context,
                player_display,
                "Challenged to a {} Battle (Rank {}, Level {})!".format(
                    printed_type, wins + 1, challenge_level
                ),
            )

    async def on_battle_start(
        self, battle_tag: str, session_data: Dict[str, Any]
    ):
        player_id = session_data.get("player_id")
        if player_id:
            db.save_session(
                player_id=player_id,
                mode=self.mode_id,
                type_id=session_data["type_id"],
                player_level=session_data["player_level"],
                challenge_level=session_data["challenge_level"],
                room_context=session_data["room_context"],
                status="in_battle",
            )
        logger.info(
            "Battle Hall match officially started in room {}".format(
                battle_tag
            )
        )

    async def on_battle_end(
        self,
        battle_tag: str,
        session_data: Dict[str, Any],
        winner: Optional[str],
    ):
        player_id = session_data["player_id"]
        player_userid = normalize_name(
            session_data.get("player_userid", "")
        )
        if not player_userid:
            p = db.get_player_by_userid(session_data.get("player_userid", ""))
            # If not in session_data directly, lookup by player_id
            conn = db.get_connection()
            c = conn.cursor()
            c.execute("SELECT userid, display_name FROM players WHERE id = ?;", (player_id,))
            row = c.fetchone()
            conn.close()
            player_userid, player_display = row if row else ("", "")
        else:
            conn = db.get_connection()
            c = conn.cursor()
            c.execute("SELECT display_name FROM players WHERE id = ?;", (player_id,))
            row = c.fetchone()
            conn.close()
            player_display = row[0] if row else player_userid

        winner_userid = normalize_name(winner) if winner else ""
        printed_type = session_data["printed_type"]
        canonical_type = session_data["canonical_type"]
        type_id = session_data["type_id"]
        player_level = session_data["player_level"]
        room_context = session_data["room_context"]

        if winner_userid == player_userid:
            # VICTORY
            db.increment_player_wins(player_id, type_id)
            new_wins = db.get_player_wins(player_id, type_id)
            total_player_wins = db.get_player_total_wins(player_id)
            db.record_match(
                player_id,
                self.mode_id,
                battle_tag,
                "Genesect",
                "win",
            )

            await self.send_reply(
                room_context,
                player_display,
                "Congratulations! You won the {} battle. Your total wins for this type is now {}/10 and your total wins for this run is now {}/180.".format(
                    printed_type, new_wins, total_player_wins
                ),
            )

            if new_wins < 10:
                # AUTO-REMATCH FOR NEXT RANK
                other_types = db.get_other_types_with_wins_count(
                    player_id, type_id
                )
                new_challenge_level = calculate_challenge_level(
                    player_level, new_wins, other_types
                )

                db.save_session(
                    player_id=player_id,
                    mode=self.mode_id,
                    type_id=type_id,
                    player_level=player_level,
                    challenge_level=new_challenge_level,
                    room_context=room_context,
                    status="active",
                )

                next_session_data = {
                    "player_id": player_id,
                    "player_userid": player_userid,
                    "type_id": type_id,
                    "printed_type": printed_type,
                    "canonical_type": canonical_type,
                    "player_level": player_level,
                    "challenge_level": new_challenge_level,
                    "room_context": room_context,
                }

                battle_config = BattleConfiguration(
                    pokemon_format="gen9battlehall",
                    team_dict=GENESECT_DICT,
                    team_packed=GENESECT_PACKED,
                    setup_commands=[
                        "/battlehalllevel {}".format(new_challenge_level),
                        "/battlehalltype {}".format(canonical_type),
                    ],
                    extra_info=next_session_data,
                )

                logger.info(
                    "Auto-challenging {} for next Battle Hall match (Rank {}, Level {})".format(
                        player_display, new_wins + 1, new_challenge_level
                    )
                )

                dispatched = (
                    await self.challenge_dispatcher.dispatch_challenge(
                        player_userid=player_userid,
                        player_display=player_display,
                        mode=self,
                        battle_config=battle_config,
                        room_context=room_context,
                        session_data=next_session_data,
                    )
                )
                if dispatched:
                    await self.send_reply(
                        room_context,
                        player_display,
                        "Challenged to a {} Battle (Rank {}, Level {})!".format(
                            printed_type, new_wins + 1, new_challenge_level
                        ),
                    )
            else:
                # 10 WINS COMPLETED FOR THIS TYPE
                db.clear_session(player_id, mode=self.mode_id)
                await self.send_reply(
                    room_context,
                    player_display,
                    "Mastery achieved! You have defeated all 10 ranks for the {} type! Use @battlehall to pick your next type.".format(
                        printed_type
                    ),
                )
        else:
            # DEFEAT OR FORFEIT
            db.reset_player_all_wins(player_id)
            db.clear_session(player_id, mode=self.mode_id)
            db.record_match(
                player_id,
                self.mode_id,
                battle_tag,
                "Genesect",
                "loss",
            )
            await self.send_reply(
                room_context,
                player_display,
                "You lost the battle! All of your Battle Hall wins have been reset to 0.",
            )
