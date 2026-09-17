"""
BattleFactoryMode — Battle Factory facility for BattleHallBot.

Game flow:
  1. Player types @factory start.
  2. Bot sends driver command /generatefactoryteam 6 to the server.
     The server replies with 6 packed sets in a chat message.
  3. Bot DMs the 6 sets (numbered 1–6) to the player.
  4. Player replies @factory draft <i> <j> <k>  (three 1-based indices).
  5. Bot records the player's 3 chosen Pokémon (in-memory).
  6. Bot generates the opponent's team with /generatefactoryteam 3.
  7. Bot uses /factoryteam <player_display>, <packed>  to register
     the player's real team with the server.
  8. Bot uses /factoryteam <bot_display>, <packed> for the opponent team.
  9. Both sides submit no team (/utm null); the server assigns the registered
     factory teams at battle start.
  10. After the battle:
      - Win: streak++, present opponent's 3 sets, player picks via
             @factory swap <opp_idx> <my_idx>  or  @factory pass.
      - Loss: streak resets, run ends.

IV upgrade on swap:
  The incoming Pokémon inherits the *replaced* player Pokémon's 6 IV stats
  with each value +2 (capped at 31).  Species/moves/item/nature/EVs come
  from the opponent Pokémon.

In-memory only — runs do NOT survive a bot restart.
"""

import asyncio
import logging
import re
import time
from copy import deepcopy
from typing import List, Optional, Dict, Any, Tuple

from config import FoulPlayConfig
import db
from fp.helpers import normalize_name
from fp.modes.base import BaseGameMode, BattleConfiguration

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_packed_team(packed: str) -> List[Dict[str, Any]]:
    """
    Parse a Showdown packed team string into a list of set dicts.
    Each mon is separated by ']'.  Fields within a mon are '|'-delimited:
      name|species|item|ability|moves|nature|evs|gender|ivs|shiny|level|happiness,...
    We keep this minimal — just enough to track and modify IVs.
    """
    sets = []
    for mon_str in packed.strip().split("]"):
        mon_str = mon_str.strip()
        if not mon_str:
            continue
        parts = mon_str.split("|")
        # Showdown packed format fields (0-indexed):
        # 0: name, 1: species, 2: item, 3: ability,
        # 4: moves (comma-separated), 5: nature, 6: EVs (HP/Atk/Def/SpA/SpD/Spe),
        # 7: gender, 8: IVs, 9: shiny, 10: level, 11: happiness/...
        entry: Dict[str, Any] = {"_raw_parts": parts}
        if len(parts) > 1:
            entry["species"] = parts[1] or parts[0]
        if len(parts) > 8:
            entry["ivs_packed"] = parts[8]
        if len(parts) > 6:
            entry["evs_packed"] = parts[6]
        sets.append(entry)
    return sets


def _ivs_from_packed(ivs_str: str) -> Dict[str, int]:
    """
    Parse EVs/IVs packed string (HP/Atk/Def/SpA/SpD/Spe).
    Empty fields default to 31 for IVs.
    """
    parts = ivs_str.split("/")
    stats = ["hp", "atk", "def", "spa", "spd", "spe"]
    result = {}
    for i, stat in enumerate(stats):
        val_str = parts[i].strip() if i < len(parts) else ""
        result[stat] = int(val_str) if val_str else 31
    return result


def _ivs_to_packed(ivs: Dict[str, int]) -> str:
    """Convert IV dict back to packed string."""
    stats = ["hp", "atk", "def", "spa", "spd", "spe"]
    return "/".join(str(ivs.get(s, 31)) for s in stats)


def _apply_iv_upgrade(
    opp_set_parts: List[str], replaced_set_parts: List[str]
) -> List[str]:
    """
    Build upgraded set parts where:
      - Species/moves/item/nature/EVs come from opp_set_parts
      - IVs come from replaced_set_parts with each +2 (capped 31)
    """
    new_parts = list(opp_set_parts)

    # Parse replaced set IVs (field 8)
    if len(replaced_set_parts) > 8:
        old_ivs_str = replaced_set_parts[8]
        old_ivs = _ivs_from_packed(old_ivs_str)
    else:
        old_ivs = {s: 31 for s in ("hp", "atk", "def", "spa", "spd", "spe")}

    # Bump each IV by 2, cap at 31
    new_ivs = {s: min(31, old_ivs.get(s, 31) + 2) for s in old_ivs}
    new_ivs_str = _ivs_to_packed(new_ivs)

    # Splice new IVs into the new set's field 8
    while len(new_parts) <= 8:
        new_parts.append("")
    new_parts[8] = new_ivs_str
    return new_parts


def _parts_to_packed_entry(parts: List[str]) -> str:
    return "|".join(parts)


def _team_from_entries(entries: List[List[str]]) -> str:
    """Join a list of part-lists back into a packed team string."""
    return "]".join(_parts_to_packed_entry(p) for p in entries)


def _format_mon_summary(parts: List[str]) -> Tuple[str, str]:
    """Extract (species, item) from Showdown packed set parts."""
    if not parts:
        return "???", "No Item"
    species = parts[1].strip() if (len(parts) > 1 and parts[1].strip()) else parts[0].strip()
    if not species:
        species = "???"
    item = parts[2].strip() if (len(parts) > 2 and parts[2].strip()) else "No Item"
    return species, item


# ---------------------------------------------------------------------------
# In-memory run state
# ---------------------------------------------------------------------------

class _FactoryRun:
    """Tracks an in-progress Battle Factory run for one player."""

    def __init__(self, player_id: int, player_display: str, lobby_room: str):
        self.player_id = player_id
        self.player_display = player_display
        self.lobby_room = lobby_room

        # 6 raw set part-lists offered to the player
        self.draft_pool: List[List[str]] = []  # [parts_list, ...]
        # 3 chosen from draft_pool (1-based index from player)
        self.player_team: List[List[str]] = []
        # Opponent's 3 sets (for post-battle swap)
        self.opponent_team: List[List[str]] = []

        self.phase = "draft"  # "draft" | "in_battle" | "post_battle"
        # Lock protecting async state
        self._lock = asyncio.Lock()

    def describe_set(self, idx_1based: int, parts: List[str]) -> str:
        """Return a human-readable description of a set."""
        species, item = _format_mon_summary(parts)
        nature = parts[5] if len(parts) > 5 and parts[5] else "?"
        moves_raw = parts[4] if len(parts) > 4 else ""
        moves = [m.strip() for m in moves_raw.split(",") if m.strip()]
        return "**{}. {}** @ {}  ({})\n   Moves: {}".format(
            idx_1based, species, item, nature, ", ".join(moves)
        )


# ---------------------------------------------------------------------------
# Mode class
# ---------------------------------------------------------------------------

class BattleFactoryMode(BaseGameMode):
    """Implements the Battle Factory facility."""

    def __init__(self, challenge_dispatcher: Any):
        self.challenge_dispatcher = challenge_dispatcher
        # Map player_userid → _FactoryRun
        self._runs: Dict[str, _FactoryRun] = {}
        # Pending bot chat messages for /generatefactoryteam replies
        # We receive the reply from the server in the room chat.
        # Map player_userid → asyncio.Future[str]
        self._generate_lock = asyncio.Lock()
        self._current_generate_future: Optional[asyncio.Future] = None
        self._pending_generate: Dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    # BaseGameMode interface
    # ------------------------------------------------------------------

    @property
    def mode_id(self) -> str:
        return "battlefactory"

    @property
    def command_prefixes(self) -> List[str]:
        return [
            "@factory",
            "@battlefactory",
            "@factoryreset",
            "@factorystats",
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
                "Battle Factory: No records yet. Use @factory start to begin."
            )
        rec = db.get_factory_record(player["id"])
        run = self._runs.get(normalize_name(player_userid))
        status_parts = [
            "**Battle Factory Records**",
            "  Current streak: {}  |  Best: {}  |  Total wins: {}".format(
                rec["current_streak"], rec["max_streak"], rec["total_wins"]
            ),
        ]
        if run:
            status_parts.append("  Active run — phase: {}".format(run.phase))
        return "\n".join(status_parts)

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel_session(
        self, player_userid: str, player_display: str, room_context: str
    ) -> str:
        norm = normalize_name(player_userid)
        self._runs.pop(norm, None)
        player = db.get_player_by_userid(player_userid)
        if player:
            db.clear_session(player["id"], mode=self.mode_id)
        msg = "Your Battle Factory run has been cancelled."
        await self.send_reply(room_context, player_display, msg)
        return msg

    # ------------------------------------------------------------------
    # prepare_battle
    # ------------------------------------------------------------------

    async def prepare_battle(self, player_userid: str) -> Optional[BattleConfiguration]:
        """
        Called just before the challenge is sent.  We send the driver
        commands here (via setup_commands) and return the Genesect dummy.
        """
        norm = normalize_name(player_userid)
        run = self._runs.get(norm)
        if run is None:
            return None

        player_packed = _team_from_entries(run.player_team)
        opp_packed = _team_from_entries(run.opponent_team)

        bot_name = getattr(FoulPlayConfig, "username", "bot") or "bot"
        setup_cmds = [
            "/factoryteam {}, {}".format(run.player_display, player_packed),
            "/factoryteam {}, {}".format(bot_name, opp_packed),
        ]

        return BattleConfiguration(
            pokemon_format="gen9battlefactory",
            team_dict=None,
            team_packed=None,
            setup_commands=setup_cmds,
            extra_info={
                "player_id": run.player_id,
                "player_userid": norm,
                "player_display": run.player_display,
                "lobby_room": run.lobby_room,
            },
        )

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    async def on_battle_start(self, battle_tag: str, session_data: Dict[str, Any]):
        norm = normalize_name(session_data.get("player_userid", ""))
        run = self._runs.get(norm)
        if run:
            run.phase = "in_battle"
        logger.info("Battle Factory battle started: {}".format(battle_tag))

    async def on_battle_end(
        self,
        battle_tag: str,
        session_data: Dict[str, Any],
        winner: Optional[str],
    ):
        from config import FoulPlayConfig

        player_id = session_data["player_id"]
        player_userid = session_data.get("player_userid", "")
        lobby_room = session_data.get("lobby_room", "")

        # Resolve display name
        conn = db.get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT userid, display_name FROM players WHERE id = ?;",
            (player_id,),
        )
        row = c.fetchone()
        conn.close()
        p_userid = row[0] if row else ""
        p_display = row[1] if row else ""

        norm = normalize_name(player_userid or p_userid)
        run = self._runs.get(norm)
        player_norm = normalize_name(p_userid)
        winner_norm = normalize_name(winner) if winner else ""

        if winner_norm == player_norm:
            # --- WIN ---
            new_streak = db.increment_factory_streak(player_id)
            db.record_match(player_id, self.mode_id, battle_tag, "Genesect", "win")

            await self.send_reply(
                lobby_room,
                p_display,
                "Battle Factory: Victory! Streak: {}".format(new_streak),
            )

            if run:
                run.phase = "post_battle"
                await self._present_swap_offer(run, p_display, lobby_room)
        else:
            # --- LOSS ---
            db.reset_factory_streak(player_id)
            db.clear_session(player_id, mode=self.mode_id)
            db.record_match(player_id, self.mode_id, battle_tag, "Genesect", "loss")
            self._runs.pop(norm, None)

            await self.send_reply(
                lobby_room,
                p_display,
                "Battle Factory: Defeated! Streak reset. Use @factory start to try again.",
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
        norm = normalize_name(player_userid)

        # --- @factoryreset ---
        if cmd == "@factoryreset":
            player_id = db.get_or_create_player(player_userid, player_display)
            db.reset_factory_all(player_id)
            db.clear_session(player_id, mode=self.mode_id)
            self._runs.pop(norm, None)
            await self.send_reply(
                room_context,
                player_display,
                "Battle Factory: All records reset.",
            )
            return

        # --- @factorystats ---
        if cmd == "@factorystats":
            status = await self.get_player_status(player_userid)
            await self.send_reply(room_context, player_display, status)
            return

        if cmd not in ("@factory", "@battlefactory"):
            return

        if not args:
            await self.send_reply(
                room_context,
                player_display,
                "Usage: @factory start | @factory draft <i> <j> <k> | "
                "@factory swap <opp_1-3> <my_1-3> | @factory pass | "
                "@factory cancel | @factory status",
            )
            return

        sub = args[0].lower()

        # --- @factory start ---
        if sub == "start":
            if norm in self._runs:
                await self.send_reply(
                    room_context,
                    player_display,
                    "You already have an active Factory run. Use @factory cancel to reset it.",
                )
                return
            player_id = db.get_or_create_player(player_userid, player_display)
            run = _FactoryRun(player_id, player_display, room_context)
            self._runs[norm] = run
            await self._start_draft(norm, player_display, room_context)
            return

        # --- @factory draft <i> <j> <k> ---
        if sub == "draft":
            run = self._runs.get(norm)
            if not run or run.phase != "draft":
                await self.send_reply(
                    room_context,
                    player_display,
                    "No active draft. Use @factory start first.",
                )
                return
            if len(args) < 4:
                await self.send_reply(
                    room_context,
                    player_display,
                    "Usage: @factory draft <1-6> <1-6> <1-6>  (three indices, no repeats)",
                )
                return
            try:
                picks = [int(a) for a in args[1:4]]
            except ValueError:
                await self.send_reply(
                    room_context,
                    player_display,
                    "Invalid indices. Example: @factory draft 1 3 5",
                )
                return
            if any(p < 1 or p > len(run.draft_pool) for p in picks):
                await self.send_reply(
                    room_context,
                    player_display,
                    "Indices must be between 1 and {}.".format(len(run.draft_pool)),
                )
                return
            if len(set(picks)) != 3:
                await self.send_reply(
                    room_context,
                    player_display,
                    "Please pick 3 different Pokémon.",
                )
                return

            run.player_team = [run.draft_pool[p - 1] for p in picks]
            player_id = run.player_id

            # Generate opponent team
            opp_packed = await self._generate_team(3, room_context, player_display)
            if not opp_packed:
                await self.send_reply(
                    room_context,
                    player_display,
                    "Error generating opponent team. Please try again.",
                )
                return
            raw_opp = []
            for mon_str in opp_packed.strip().split("]"):
                mon_str = mon_str.strip()
                if mon_str:
                    raw_opp.append(mon_str.split("|"))
            run.opponent_team = raw_opp

            run.phase = "in_battle"

            # Save session
            db.save_session(
                player_id=player_id,
                mode=self.mode_id,
                type_id=None,
                player_level=50,
                challenge_level=0,
                room_context=room_context,
                status="active",
            )

            await self.send_reply(
                room_context,
                player_display,
                "Drafting Pokémon {}! Challenge incoming…".format(
                    ", ".join(str(p) for p in picks)
                ),
            )

            # Build config and dispatch
            player_packed = _team_from_entries(run.player_team)
            opp_packed_final = _team_from_entries(run.opponent_team)
            bot_name = getattr(FoulPlayConfig, "username", "bot") or "bot"
            config = BattleConfiguration(
                pokemon_format="gen9battlefactory",
                team_dict=None,
                team_packed=None,
                setup_commands=[
                    "/factoryteam {}, {}".format(player_display, player_packed),
                    "/factoryteam {}, {}".format(bot_name, opp_packed_final),
                ],
                extra_info={
                    "player_id": player_id,
                    "player_userid": norm,
                    "player_display": player_display,
                    "lobby_room": room_context,
                },
            )
            session_data = {
                "player_id": player_id,
                "player_userid": norm,
                "player_display": player_display,
                "lobby_room": room_context,
            }

            await self.challenge_dispatcher.dispatch_challenge(
                player_userid=player_userid,
                player_display=player_display,
                mode=self,
                battle_config=config,
                room_context=room_context,
                session_data=session_data,
            )
            return

        # --- @factory swap <opp_idx> <my_idx> ---
        if sub == "swap":
            run = self._runs.get(norm)
            if not run or run.phase != "post_battle":
                await self.send_reply(
                    room_context,
                    player_display,
                    "No pending swap offer. Win a battle first.",
                )
                return
            if len(args) < 3:
                await self.send_reply(
                    room_context,
                    player_display,
                    "Usage: @factory swap <opp_1-3> <my_1-3>",
                )
                return
            try:
                opp_idx = int(args[1])
                my_idx = int(args[2])
            except ValueError:
                await self.send_reply(
                    room_context, player_display, "Invalid indices."
                )
                return

            if opp_idx < 1 or opp_idx > len(run.opponent_team):
                await self.send_reply(
                    room_context, player_display,
                    "Opponent index must be 1–{}.".format(len(run.opponent_team))
                )
                return
            if my_idx < 1 or my_idx > len(run.player_team):
                await self.send_reply(
                    room_context, player_display,
                    "Your index must be 1–{}.".format(len(run.player_team))
                )
                return

            opp_parts = run.opponent_team[opp_idx - 1]
            my_parts = run.player_team[my_idx - 1]
            upgraded = _apply_iv_upgrade(opp_parts, my_parts)
            run.player_team[my_idx - 1] = upgraded

            species, _ = _format_mon_summary(opp_parts)
            await self.send_reply(
                room_context,
                player_display,
                "Swapped! {} joined your team with upgraded IVs. "
                "Generating next opponent…".format(species),
            )
            await self._next_battle(norm, player_userid, player_display, run, room_context)
            return

        # --- @factory pass ---
        if sub == "pass":
            run = self._runs.get(norm)
            if not run or run.phase != "post_battle":
                await self.send_reply(
                    room_context,
                    player_display,
                    "No pending swap offer.",
                )
                return
            await self.send_reply(
                room_context, player_display, "Keeping your team. Generating next opponent…"
            )
            await self._next_battle(norm, player_userid, player_display, run, room_context)
            return

        # --- @factory cancel ---
        if sub in ("cancel", "quit"):
            await self.cancel_session(player_userid, player_display, room_context)
            return

        # --- @factory status ---
        if sub == "status":
            status = await self.get_player_status(player_userid)
            await self.send_reply(room_context, player_display, status)
            return

        await self.send_reply(
            room_context,
            player_display,
            "Unknown sub-command '{}'. "
            "Use: start | draft | swap | pass | cancel | status".format(sub),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _start_draft(
        self, player_norm: str, player_display: str, room_context: str
    ):
        """Generate 6 sets and present them to the player."""
        packed = await self._generate_team(6, room_context, player_display)
        if not packed:
            await self.send_reply(
                room_context,
                player_display,
                "Error generating draft pool. Please try again.",
            )
            self._runs.pop(player_norm, None)
            return

        run = self._runs.get(player_norm)
        if not run:
            return

        # Parse packed into list-of-parts
        run.draft_pool = []
        for mon_str in packed.strip().split("]"):
            mon_str = mon_str.strip()
            if mon_str:
                run.draft_pool.append(mon_str.split("|"))

        # Return each Pokémon as a message before prompting the user to draft
        for i, parts in enumerate(run.draft_pool, start=1):
            species, item = _format_mon_summary(parts)
            nature = parts[5] if len(parts) > 5 and parts[5] else "?"
            moves_raw = parts[4] if len(parts) > 4 else ""
            moves = [m.strip() for m in moves_raw.split(",") if m.strip()]
            mon_msg = "{}. {} @ {}  ({}) — {}".format(
                i, species, item, nature, ", ".join(moves)
            )
            await self.challenge_dispatcher.send_reply("", player_display, mon_msg)

        prompt_msg = "**Battle Factory Draft** — choose 3 with @factory draft <i> <j> <k>:"
        await self.challenge_dispatcher.send_reply("", player_display, prompt_msg)

    async def _present_swap_offer(
        self, run: _FactoryRun, player_display: str, lobby_room: str
    ):
        """After a win, show the opponent's 3 Pokémon and ask for a swap."""
        await self.challenge_dispatcher.send_reply(
            "", player_display,
            "**Battle Factory: Swap Offer** — You may swap one of your Pokémon for one of the opponent's:"
        )
        await self.challenge_dispatcher.send_reply("", player_display, "**Opponent's Pokémon:**")
        for i, parts in enumerate(run.opponent_team, start=1):
            species, item = _format_mon_summary(parts)
            await self.challenge_dispatcher.send_reply(
                "", player_display, "  {}. {}  @ {}".format(i, species, item)
            )

        await self.challenge_dispatcher.send_reply("", player_display, "**Your current team:**")
        for i, parts in enumerate(run.player_team, start=1):
            species, item = _format_mon_summary(parts)
            await self.challenge_dispatcher.send_reply(
                "", player_display, "  {}. {}  @ {}".format(i, species, item)
            )

        await self.challenge_dispatcher.send_reply(
            "", player_display,
            "Use **@factory swap <opp_#> <your_#>** to swap, or **@factory pass** to keep your team."
        )

    async def _next_battle(
        self,
        player_norm: str,
        player_userid: str,
        player_display: str,
        run: _FactoryRun,
        lobby_room: str,
    ):
        """Generate a new opponent and dispatch the next challenge."""
        opp_packed = await self._generate_team(3, lobby_room, player_display)
        if not opp_packed:
            await self.send_reply(
                lobby_room,
                player_display,
                "Error generating opponent team. Please try again.",
            )
            return

        raw_opp = []
        for mon_str in opp_packed.strip().split("]"):
            mon_str = mon_str.strip()
            if mon_str:
                raw_opp.append(mon_str.split("|"))
        run.opponent_team = raw_opp
        run.phase = "in_battle"
        player_packed = _team_from_entries(run.player_team)
        opp_packed_final = _team_from_entries(run.opponent_team)
        bot_name = getattr(FoulPlayConfig, "username", "bot") or "bot"
        config = BattleConfiguration(
            pokemon_format="gen9battlefactory",
            team_dict=None,
            team_packed=None,
            setup_commands=[
                "/factoryteam {}, {}".format(player_display, player_packed),
                "/factoryteam {}, {}".format(bot_name, opp_packed_final),
            ],
            extra_info={
                "player_id": run.player_id,
                "player_userid": player_norm,
                "player_display": player_display,
                "lobby_room": lobby_room,
            },
        )
        session_data = {
            "player_id": run.player_id,
            "player_userid": player_norm,
            "player_display": player_display,
            "lobby_room": lobby_room,
        }

        await self.challenge_dispatcher.dispatch_challenge(
            player_userid=player_userid,
            player_display=player_display,
            mode=self,
            battle_config=config,
            room_context=lobby_room,
            session_data=session_data,
        )

    async def _generate_team(
        self, count: int, room_context: str, player_display: str
    ) -> Optional[str]:
        """Send /generatefactoryteam <count> to the server and wait for the
        server's reply (which contains the packed team string).

        Because the server reply does not include a request ID or recipient,
        we serialize generation with an asyncio.Lock to avoid race conditions,
        cross-talk, or team mixing when multiple players start or draft concurrently.
        """
        async with self._generate_lock:
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            self._current_generate_future = fut
            key = "_gen_{}_{}_{}".format(count, id(self), time.time())
            self._pending_generate[key] = fut

            try:
                await self.challenge_dispatcher.ps_websocket_client.send_message(
                    "", ["/generatefactoryteam {}".format(count)]
                )
                # Wait up to 15 seconds for the server's reply
                packed = await asyncio.wait_for(fut, timeout=15.0)
                return packed
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for /generatefactoryteam reply")
                return None
            finally:
                self._pending_generate.pop(key, None)
                if self._current_generate_future is fut:
                    self._current_generate_future = None

    def notify_factory_generate_reply(self, packed_team: str):
        """Called by CommandDispatcher when it intercepts a packed-team reply

        from the server (the response to /generatefactoryteam).
        Resolves the active Future.
        """
        if self._current_generate_future and not self._current_generate_future.done():
            self._current_generate_future.set_result(packed_team)
        for key, fut in list(self._pending_generate.items()):
            if not fut.done():
                fut.set_result(packed_team)
                return
