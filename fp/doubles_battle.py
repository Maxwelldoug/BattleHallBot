"""
Heuristic doubles battler for gen9battletowerdoubles.

Design: For each active Pokémon slot on our side, pick the best available
move + target combination.  Score = base_power × type_effectiveness.

Target slots (Showdown doubles protocol):
  -1 = opponent slot 1
  -2 = opponent slot 2
   1 = ally slot 1  (self if we ARE slot 1)
   2 = ally slot 2  (self if we ARE slot 2)

The four targeting permutations evaluated:
  straight_across – slot0→opp0, slot1→opp1
  diagonal        – slot0→opp1, slot1→opp0
  focus_opp0      – slot0→opp0, slot1→opp0
  focus_opp1      – slot0→opp1, slot1→opp1

We pick the permutation with the highest *total* score.

Message format we send back:
  ["/choose move <idx> <target>, move <idx> <target>|<rqid>"]
"""

import json
import logging
import re
from typing import Optional, Dict, List, Tuple, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type chart (Gen 6+, including Fairy)
# ---------------------------------------------------------------------------
_TYPE_CHART: Dict[str, Dict[str, float]] = {
    "normal":   {"rock": 0.5, "ghost": 0.0, "steel": 0.5},
    "fire":     {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 2.0,
                 "bug": 2.0, "rock": 0.5, "dragon": 0.5, "steel": 2.0},
    "water":    {"fire": 2.0, "water": 0.5, "grass": 0.5, "ground": 2.0,
                 "rock": 2.0, "dragon": 0.5},
    "electric": {"water": 2.0, "electric": 0.5, "grass": 0.5, "ground": 0.0,
                 "flying": 2.0, "dragon": 0.5},
    "grass":    {"fire": 0.5, "water": 2.0, "grass": 0.5, "poison": 0.5,
                 "ground": 2.0, "flying": 0.5, "bug": 0.5, "rock": 2.0,
                 "dragon": 0.5, "steel": 0.5},
    "ice":      {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 0.5,
                 "ground": 2.0, "flying": 2.0, "dragon": 2.0, "steel": 0.5},
    "fighting": {"normal": 2.0, "ice": 2.0, "poison": 0.5, "flying": 0.5,
                 "psychic": 0.5, "bug": 0.5, "rock": 2.0, "ghost": 0.0,
                 "dark": 2.0, "steel": 2.0, "fairy": 0.5},
    "poison":   {"grass": 2.0, "poison": 0.5, "ground": 0.5, "rock": 0.5,
                 "ghost": 0.5, "steel": 0.0, "fairy": 2.0},
    "ground":   {"fire": 2.0, "electric": 2.0, "grass": 0.5, "poison": 2.0,
                 "flying": 0.0, "bug": 0.5, "rock": 2.0, "steel": 2.0},
    "flying":   {"electric": 0.5, "grass": 2.0, "fighting": 2.0, "bug": 2.0,
                 "rock": 0.5, "steel": 0.5},
    "psychic":  {"fighting": 2.0, "poison": 2.0, "psychic": 0.5,
                 "dark": 0.0, "steel": 0.5},
    "bug":      {"fire": 0.5, "grass": 2.0, "fighting": 0.5, "poison": 0.5,
                 "flying": 0.5, "psychic": 2.0, "ghost": 0.5, "dark": 2.0,
                 "steel": 0.5, "fairy": 0.5},
    "rock":     {"fire": 2.0, "ice": 2.0, "fighting": 0.5, "ground": 0.5,
                 "flying": 2.0, "bug": 2.0, "steel": 0.5},
    "ghost":    {"normal": 0.0, "psychic": 2.0, "ghost": 2.0, "dark": 0.5},
    "dragon":   {"dragon": 2.0, "steel": 0.5, "fairy": 0.0},
    "dark":     {"fighting": 0.5, "psychic": 2.0, "ghost": 2.0, "dark": 0.5,
                 "fairy": 0.5},
    "steel":    {"fire": 0.5, "water": 0.5, "electric": 0.5, "ice": 2.0,
                 "rock": 2.0, "steel": 0.5, "fairy": 2.0},
    "fairy":    {"fire": 0.5, "fighting": 2.0, "poison": 0.5, "dragon": 2.0,
                 "dark": 2.0, "steel": 0.5},
}

# Status/non-damaging moves have basePower == 0 in the JSON
_STRUGGLE_BP = 50  # fallback if only Struggle is available


def _type_effectiveness(move_type: str, defender_types: List[str]) -> float:
    """Product of single-type matchups."""
    effectiveness = 1.0
    chart = _TYPE_CHART.get(move_type.lower(), {})
    for dtype in defender_types:
        effectiveness *= chart.get(dtype.lower(), 1.0)
    return effectiveness


def _move_score(
    base_power: int,
    move_type: str,
    defender_types: List[str],
    is_spread: bool = False,
) -> float:
    """Score a move against a specific defender."""
    if base_power <= 0:
        return 0.0
    eff = _type_effectiveness(move_type, defender_types)
    bp = base_power
    # Spread moves (e.g. Surf, Earthquake) hit all adjacent; small bonus
    # because they can hit both opponents simultaneously.
    if is_spread:
        bp = int(bp * 0.75)  # Showdown halves spread in doubles
    return float(bp) * eff


def _get_defender_types(species_id: str, pokedex: Dict) -> List[str]:
    """Look up types for a Pokémon species from the pokedex dict."""
    entry = pokedex.get(species_id.lower(), {})
    types = entry.get("types", [])
    return [t.lower() for t in types] if types else ["normal"]


# Targets that are directed at the opponent side
_OPPONENT_TARGETS = {"normal", "any", "allAdjacentFoes", "allAdjacent"}
# Spread moves (hit multiple targets)
_SPREAD_TARGETS = {"allAdjacentFoes", "allAdjacent"}
# Self / ally targeting — skip these when selecting an attack
_SELF_TARGETS = {"self", "adjacentAlly", "adjacentAllyOrSelf", "allyTeam", "allySide"}


class DoublesHeuristicBattler:
    """
    Stateless heuristic battler for doubles battles.

    Usage:
        battler = DoublesHeuristicBattler(all_move_json, pokedex)
        choice_str = battler.pick_move(request_json, opponent_slots)
    """

    def __init__(self, all_move_json: Dict, pokedex: Dict):
        self.moves = all_move_json  # id → move data dict
        self.pokedex = pokedex     # id → pkmn data dict

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pick_move(
        self,
        request_json: Dict,
        opp_species: List[Optional[str]],
        rqid: Optional[int] = None,
    ) -> List[str]:
        """
        Given the |request| JSON and a list of opponent species (len 2, None if fainted),
        return a Showdown choose string for doubles.

        Args:
            request_json: parsed JSON from the |request| message
            opp_species: [opp_slot0_species, opp_slot1_species]
                         Use None if a slot is fainted / empty.
            rqid: the request ID (appended to the message)

        Returns:
            ["/choose <slot0_action>, <slot1_action>|<rqid>"]  — ready to send.
        """
        active = request_json.get("active", [])
        side_pokemon = request_json.get("side", {}).get("pokemon", [])

        # Build list of live opponent types
        opp_types: List[Optional[List[str]]] = []
        for sp in opp_species:
            if sp is None:
                opp_types.append(None)
            else:
                opp_types.append(_get_defender_types(sp, self.pokedex))

        # Pick action for each active slot
        actions = []
        for slot_idx, active_data in enumerate(active[:2]):
            if not active_data:
                actions.append("pass")
                continue

            # If we're forced to switch or must pass
            if active_data.get("trapped") is False and active_data.get("maybeTrapped") is False:
                pass  # normal case

            action = self._best_action_for_slot(
                slot_idx, active_data, opp_types, side_pokemon
            )
            actions.append(action)

        # Pad to 2 slots if needed
        while len(actions) < 2:
            actions.append("pass")

        rqid_str = "|{}".format(rqid) if rqid is not None else ""
        choice = "/choose {}, {}{}".format(actions[0], actions[1], rqid_str)
        return [choice]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _best_action_for_slot(
        self,
        slot_idx: int,
        active_data: Dict,
        opp_types: List[Optional[List[str]]],
        side_pokemon: List[Dict],
    ) -> str:
        """Pick the best move string for a single active slot."""
        moves_info = active_data.get("moves", [])
        if not moves_info:
            return "move 1 -1"

        # Gather usable moves with their scores against each opponent
        # Score per move per target: (move_index_1based, target_slot, score)
        best_score = -1.0
        best_action = "move 1 -1"

        live_opp_slots = [
            -(i + 1) for i, t in enumerate(opp_types) if t is not None
        ]
        if not live_opp_slots:
            # No live opponents — shouldn't happen mid-battle but be safe
            live_opp_slots = [-1]

        for mi, move_data in enumerate(moves_info, start=1):
            if move_data.get("disabled", False):
                continue
            if move_data.get("pp", 1) <= 0:
                continue

            move_id = move_data.get("id", "")
            move_entry = self.moves.get(move_id, {})
            base_power = move_entry.get("basePower", 0)
            move_type = move_entry.get("type", "normal").lower()
            target = move_entry.get("target", "normal")

            # Skip non-damaging moves entirely for scoring
            if base_power <= 0:
                continue

            # Skip self/ally-targeted moves
            if target in _SELF_TARGETS:
                continue

            is_spread = target in _SPREAD_TARGETS

            if is_spread:
                # Spread move hits all live opponents simultaneously
                total = sum(
                    _move_score(base_power, move_type, opp_types[abs(s) - 1], is_spread=True)
                    for s in live_opp_slots
                    if opp_types[abs(s) - 1] is not None
                )
                # Use -1 as canonical target for spread (server accepts this)
                target_slot = -1
                score = total
            else:
                # Single-target: find best opponent
                best_t = -1
                best_t_score = -1.0
                for ts in live_opp_slots:
                    ot = opp_types[abs(ts) - 1]
                    if ot is None:
                        continue
                    s = _move_score(base_power, move_type, ot)
                    if s > best_t_score:
                        best_t_score = s
                        best_t = ts
                if best_t == -1:
                    best_t = live_opp_slots[0]
                target_slot = best_t
                score = best_t_score

            if score > best_score:
                best_score = score
                best_action = "move {} {}".format(mi, target_slot)

        # If no damaging move found (all status), just use move 1 against slot -1
        if best_score < 0:
            best_action = "move 1 {}".format(live_opp_slots[0])

        return best_action


# ---------------------------------------------------------------------------
# Integration: async doubles battle loop
# ---------------------------------------------------------------------------

async def doubles_pokemon_battle(
    ps_websocket_client,
    pokemon_battle_type: str,
    team_dict,
    battle_tag: str,
):
    """
    Drives a doubles battle to completion using the heuristic battler.

    Returns the winner username string (or None on tie).
    """
    from fp.run_battle import battle_is_finished, start_standard_battle
    from data import all_move_json, pokedex
    import constants

    battler = DoublesHeuristicBattler(all_move_json, pokedex)
    battle = None

    # Opponent species tracker: [slot0_species, slot1_species]
    opp_species: List[Optional[str]] = [None, None]

    try:
        battle = await start_standard_battle(
            ps_websocket_client,
            pokemon_battle_type,
            team_dict,
            battle_tag=battle_tag,
        )

        while True:
            msg = await ps_websocket_client.receive_message(room=battle.battle_tag)

            # Parse any species reveals from the message
            _update_opp_species(msg, opp_species)

            if battle_is_finished(battle.battle_tag, msg):
                winner = (
                    msg.split(constants.WIN_STRING)[-1].split("\n")[0].strip()
                    if constants.WIN_STRING in msg
                    else None
                )
                logger.info("[{}] Doubles winner: {}".format(battle.battle_tag, winner))
                await ps_websocket_client.leave_battle(battle.battle_tag)
                return winner

            # Parse request JSON from the message
            request_json = _extract_request_json(msg)
            if request_json and not request_json.get("wait") and not request_json.get("teamPreview"):
                rqid = request_json.get("rqid")
                choice = battler.pick_move(request_json, opp_species, rqid=rqid)
                await ps_websocket_client.send_message(battle.battle_tag, choice)

    finally:
        if battle is not None and getattr(battle, "file_log_handler", None) is not None:
            import logging as _logging
            _logging.getLogger().removeHandler(battle.file_log_handler)


def _extract_request_json(msg: str) -> Optional[Dict]:
    """Extract and parse the first |request| JSON blob from a message."""
    for line in msg.split("\n"):
        if line.startswith("|request|"):
            payload = line[len("|request|"):]
            if not payload.strip():
                return None
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("Failed to parse request JSON: %s", payload[:120])
                return None
    return None


# Regex to capture species from |switch| or |drag| lines
_SWITCH_RE = re.compile(r"^\|(?:switch|drag)\|p(\d+)a: [^|]+\|([^,|]+)")


def _update_opp_species(msg: str, opp_species: List[Optional[str]]) -> None:
    """
    Parse switch/drag lines to record the opponent's active Pokémon species.

    Showdown sends:  |switch|p2a: nickname|Species, Lv50|hp/maxhp
    We track p2a (slot 1) and p2b (slot 2) by scanning for the player slot.
    """
    for line in msg.split("\n"):
        # Check for opponent switch lines (p2a or p2b)
        if "|switch|p2" in line or "|drag|p2" in line:
            # Extract sub-slot letter (a or b) and species
            # Pattern: |switch|p2a: Nickname|Charizard, Lv50|...
            parts = line.split("|")
            if len(parts) < 5:
                continue
            slot_field = parts[2]  # e.g. "p2a: Nickname"
            species_field = parts[3]  # e.g. "Charizard, Lv50"
            species_raw = species_field.split(",")[0].strip().lower().replace(" ", "").replace("-", "")
            if "a:" in slot_field:
                opp_species[0] = species_raw
            elif "b:" in slot_field:
                opp_species[1] = species_raw
        # Track fainting
        if "|faint|p2" in line:
            parts = line.split("|")
            if len(parts) >= 3:
                slot_field = parts[2]
                if "a:" in slot_field:
                    opp_species[0] = None
                elif "b:" in slot_field:
                    opp_species[1] = None
