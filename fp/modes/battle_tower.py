"""
BattleTowerMode — Battle Tower facility for BattleHallBot.

Commands:
  @battletower single   — start/resume a singles run
  @battletower double   — start/resume a doubles run
  @battletower          — resume active run (either format)
  @towerstats           — show streak records
  @towerreset           — fully reset records and session
  @towercancel / @cancel — cancel pending challenge (preserves streak)

Game flow:
  • 7 opponents per "room".  The bot uses /battletowerroom <n> to signal
    which room (1–7) to the server, and the server generates an opponent
    team at the matching difficulty level.
  • On victory: streak increments, auto-challenge next opponent.
    After 7 wins (room complete), the server difficulty advances automatically
    via the room number increment sent by the bot.
  • On defeat: streak resets to 0.  Session is cleared.
  • The player's own team is replaced server-side (Genesect dummy → real team
    via Adjust Level = 50 in the format ruleset).
"""

import logging
from typing import List, Optional, Dict, Any

import db
from fp.helpers import normalize_name
from fp.modes.base import BaseGameMode, BattleConfiguration
from teams.team_converter import json_to_packed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dummy team — 3 Genesects for singles, 4 for doubles.
# The server replaces them with the real player team at battle start.
# ---------------------------------------------------------------------------

def _make_genesect_entry() -> Dict[str, Any]:
    return {
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
        "evs": {"hp": "0", "atk": "252", "def": "4", "spa": "0", "spd": "0", "spe": "252"},
        "happiness": 255,
    }


TOWER_SINGLES_DICT = [_make_genesect_entry() for _ in range(3)]
TOWER_SINGLES_PACKED = json_to_packed(TOWER_SINGLES_DICT)

TOWER_DOUBLES_DICT = [_make_genesect_entry() for _ in range(4)]
TOWER_DOUBLES_PACKED = json_to_packed(TOWER_DOUBLES_DICT)

# Opponents per room before the room clears
BATTLES_PER_ROOM = 7
# Maximum room number
MAX_ROOM = 7


def _room_for_streak(streak: int) -> int:
    """
    Convert a 0-based win streak into the current room number (1–MAX_ROOM).
    Room 1: streak 0–6 (wins 1–7)
    Room 2: streak 7–13 (wins 8–14) … etc.
    """
    return min((streak // BATTLES_PER_ROOM) + 1, MAX_ROOM)


class BattleTowerMode(BaseGameMode):
    """Implements the Battle Tower facility (singles and doubles variants)."""

    def __init__(self, challenge_dispatcher: Any):
        self.challenge_dispatcher = challenge_dispatcher

    # ------------------------------------------------------------------
    # BaseGameMode interface
    # ------------------------------------------------------------------

    @property
    def mode_id(self) -> str:
        return "battletower"

    @property
    def command_prefixes(self) -> List[str]:
        return [
            "@battletower",
            "@bt",
            "@towerstats",
            "@towerreset",
            "@towercancel",
        ]

    async def send_reply(self, room: str, player_display: str, msg_text: str):
        await self.challenge_dispatcher.send_reply(room, player_display, msg_text)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_player_status(self, player_userid: str) -> str:
        player = db.get_player_by_userid(player_userid)
        if not player:
            return (
                "Battle Tower: No records yet. Use @battletower single or "
                "@battletower double to start."
            )
        player_id = player["id"]
        records = db.get_tower_all_records(player_id)
        session = db.get_session(player_id, mode=self.mode_id)
        lines = ["**Battle Tower Records**"]
        for fmt in ("singles", "doubles"):
            rec = records[fmt]
            lines.append(
                "  {} — Current streak: {}  |  Best: {}".format(
                    fmt.capitalize(), rec["current_streak"], rec["max_streak"]
                )
            )
        if session:
            ctx = session.get("room_context", "")
            fmt_type = session.get("room_context", "")
            # room_context holds the format_type for tower
            fmt_str = session.get("challenge_level", "")  # misused field for room#
            lines.append("  Active run in progress.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel_session(
        self, player_userid: str, player_display: str, room_context: str
    ) -> str:
        player = db.get_player_by_userid(player_userid)
        if player:
            db.clear_session(player["id"], mode=self.mode_id)
        msg = "Your Battle Tower session was cancelled (streak preserved)."
        await self.send_reply(room_context, player_display, msg)
        return msg

    # ------------------------------------------------------------------
    # prepare_battle (called by ChallengeDispatcher before each challenge)
    # ------------------------------------------------------------------

    async def prepare_battle(self, player_userid: str) -> Optional[BattleConfiguration]:
        player = db.get_player_by_userid(player_userid)
        if not player:
            return None
        session = db.get_session(player["id"], mode=self.mode_id)
        if not session:
            return None

        fmt_type = session["room_context"]         # 'singles' or 'doubles'
        room_num = session["challenge_level"] or 1  # re-used field

        if fmt_type == "doubles":
            pkmn_format = "gen9battletowerdoubles"
            team_dict = TOWER_DOUBLES_DICT
            team_packed = TOWER_DOUBLES_PACKED
        else:
            pkmn_format = "gen9battletower"
            team_dict = TOWER_SINGLES_DICT
            team_packed = TOWER_SINGLES_PACKED

        return BattleConfiguration(
            pokemon_format=pkmn_format,
            team_dict=team_dict,
            team_packed=team_packed,
            setup_commands=["/battletowerroom {}".format(room_num)],
            extra_info={
                "player_id": player["id"],
                "format_type": fmt_type,
                "room_num": room_num,
                "room_context": session.get("room_context", ""),
                "lobby_room": session.get("room_context", ""),
            },
        )

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    async def on_battle_start(self, battle_tag: str, session_data: Dict[str, Any]):
        logger.info(
            "Battle Tower battle started: {} (room {}, {})".format(
                battle_tag,
                session_data.get("room_num"),
                session_data.get("format_type"),
            )
        )

    async def on_battle_end(
        self,
        battle_tag: str,
        session_data: Dict[str, Any],
        winner: Optional[str],
    ):
        from config import FoulPlayConfig

        player_id = session_data["player_id"]
        fmt_type = session_data["format_type"]
        lobby_room = session_data.get("lobby_room", "")

        # Resolve player display name
        conn = db.get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT userid, display_name FROM players WHERE id = ?;", (player_id,)
        )
        row = c.fetchone()
        conn.close()
        player_userid = row[0] if row else ""
        player_display = row[1] if row else ""

        bot_userid = normalize_name(FoulPlayConfig.username)
        winner_userid = normalize_name(winner) if winner else ""
        player_norm = normalize_name(player_userid)

        if winner_userid == player_norm:
            # --- VICTORY ---
            new_streak = db.increment_tower_streak(player_id, fmt_type)
            room_num = _room_for_streak(new_streak)
            db.record_match(player_id, self.mode_id, battle_tag, "Genesect", "win")

            wins_in_room = new_streak % BATTLES_PER_ROOM
            if wins_in_room == 0:
                wins_in_room = BATTLES_PER_ROOM  # just completed the room

            await self.send_reply(
                lobby_room,
                player_display,
                (
                    "Battle Tower: Victory! Streak: {} "
                    "(Room {}, Battle {}/{})".format(
                        new_streak, room_num, wins_in_room, BATTLES_PER_ROOM
                    )
                ),
            )

            # Auto-challenge next opponent
            next_room = _room_for_streak(new_streak)
            await self._dispatch_next(
                player_userid, player_display, player_id,
                fmt_type, next_room, lobby_room
            )
        else:
            # --- DEFEAT ---
            db.reset_tower_streak(player_id, fmt_type)
            db.clear_session(player_id, mode=self.mode_id)
            db.record_match(player_id, self.mode_id, battle_tag, "Genesect", "loss")

            await self.send_reply(
                lobby_room,
                player_display,
                "Battle Tower: Defeated! Your streak has been reset to 0.",
            )

    # ------------------------------------------------------------------
    # Command handler
    # ------------------------------------------------------------------

    async def handle_command(
        self,
        player_userid: str,
        player_display: str,
        command: str,
        args: List[str],
        room_context: str,
    ):
        cmd = command.lower()

        # --- @towerreset ---
        if cmd == "@towerreset":
            player_id = db.get_or_create_player(player_userid, player_display)
            db.reset_tower_all(player_id)
            db.clear_session(player_id, mode=self.mode_id)
            await self.send_reply(
                room_context,
                player_display,
                "Battle Tower: All records and streak have been reset.",
            )
            return

        # --- @towerstats ---
        if cmd == "@towerstats":
            status = await self.get_player_status(player_userid)
            await self.send_reply(room_context, player_display, status)
            return

        # --- @towercancel ---
        if cmd == "@towercancel":
            bm = getattr(self.challenge_dispatcher, "battle_manager", None)
            if bm:
                bm.remove_from_wait_queue(player_userid)
                await bm.cancel_pending_challenge(player_userid)
            await self.cancel_session(player_userid, player_display, room_context)
            return

        # --- @battletower [single|double] or just @battletower to resume ---
        if cmd in ("@battletower", "@bt"):
            if not args:
                # Resume existing run
                player = db.get_player_by_userid(player_userid)
                if not player:
                    await self.send_reply(
                        room_context,
                        player_display,
                        "No active run. Use @battletower single or @battletower double.",
                    )
                    return
                session = db.get_session(player["id"], mode=self.mode_id)
                if not session:
                    await self.send_reply(
                        room_context,
                        player_display,
                        "No active run. Use @battletower single or @battletower double.",
                    )
                    return
                fmt_type = session["room_context"]
                room_num = session["challenge_level"] or 1
                await self._issue_challenge(
                    player_userid, player_display, player["id"],
                    fmt_type, room_num, room_context
                )
                return

            sub = args[0].lower()
            if sub not in ("single", "singles", "double", "doubles"):
                await self.send_reply(
                    room_context,
                    player_display,
                    "Usage: @battletower single | @battletower double",
                )
                return

            fmt_type = "singles" if sub.startswith("single") else "doubles"
            player_id = db.get_or_create_player(player_userid, player_display)
            record = db.get_tower_record(player_id, fmt_type)
            current_streak = record["current_streak"]
            room_num = _room_for_streak(current_streak)

            # Save session so prepare_battle can reconstruct it
            db.save_session(
                player_id=player_id,
                mode=self.mode_id,
                type_id=None,
                player_level=50,
                challenge_level=room_num,
                room_context=fmt_type,   # we hijack room_context to store format_type
                status="active",
            )

            await self._issue_challenge(
                player_userid, player_display, player_id,
                fmt_type, room_num, room_context
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _build_config(
        self, fmt_type: str, player_id: int, room_num: int, lobby_room: str
    ) -> BattleConfiguration:
        if fmt_type == "doubles":
            pkmn_format = "gen9battletowerdoubles"
            team_dict = TOWER_DOUBLES_DICT
            team_packed = TOWER_DOUBLES_PACKED
        else:
            pkmn_format = "gen9battletower"
            team_dict = TOWER_SINGLES_DICT
            team_packed = TOWER_SINGLES_PACKED

        return BattleConfiguration(
            pokemon_format=pkmn_format,
            team_dict=team_dict,
            team_packed=team_packed,
            setup_commands=["/battletowerroom {}".format(room_num)],
            extra_info={
                "player_id": player_id,
                "format_type": fmt_type,
                "room_num": room_num,
                "lobby_room": lobby_room,
            },
        )

    async def _issue_challenge(
        self,
        player_userid: str,
        player_display: str,
        player_id: int,
        fmt_type: str,
        room_num: int,
        lobby_room: str,
    ):
        record = db.get_tower_record(player_id, fmt_type)
        config = await self._build_config(fmt_type, player_id, room_num, lobby_room)
        session_data = {
            "player_id": player_id,
            "player_userid": player_userid,
            "format_type": fmt_type,
            "room_num": room_num,
            "lobby_room": lobby_room,
        }

        dispatched = await self.challenge_dispatcher.dispatch_challenge(
            player_userid=player_userid,
            player_display=player_display,
            mode=self,
            battle_config=config,
            room_context=lobby_room,
            session_data=session_data,
        )
        if dispatched:
            await self.send_reply(
                lobby_room,
                player_display,
                "Battle Tower: Challenging {} Room {} (streak: {})!".format(
                    fmt_type.capitalize(), room_num, record["current_streak"]
                ),
            )

    async def _dispatch_next(
        self,
        player_userid: str,
        player_display: str,
        player_id: int,
        fmt_type: str,
        room_num: int,
        lobby_room: str,
    ):
        """Auto-challenge next opponent after a win."""
        # Update session with latest room number
        db.save_session(
            player_id=player_id,
            mode=self.mode_id,
            type_id=None,
            player_level=50,
            challenge_level=room_num,
            room_context=fmt_type,
            status="active",
        )
        config = await self._build_config(fmt_type, player_id, room_num, lobby_room)
        session_data = {
            "player_id": player_id,
            "player_userid": player_userid,
            "format_type": fmt_type,
            "room_num": room_num,
            "lobby_room": lobby_room,
        }
        dispatched = await self.challenge_dispatcher.dispatch_challenge(
            player_userid=player_userid,
            player_display=player_display,
            mode=self,
            battle_config=config,
            room_context=lobby_room,
            session_data=session_data,
        )
        if dispatched:
            record = db.get_tower_record(player_id, fmt_type)
            await self.send_reply(
                lobby_room,
                player_display,
                "Battle Tower: Challenging Room {} next!".format(room_num),
            )
