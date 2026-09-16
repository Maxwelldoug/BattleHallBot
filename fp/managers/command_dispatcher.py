import asyncio
import logging
from typing import Dict, Optional, List
from config import FoulPlayConfig
from fp.helpers import normalize_name
from fp.modes.base import BaseGameMode
from fp.websocket_client import PSWebsocketClient

logger = logging.getLogger(__name__)


class CommandDispatcher:
    def __init__(
        self,
        ps_websocket_client: PSWebsocketClient,
        battle_manager: Any = None,
        challenge_dispatcher: Any = None,
    ):
        self.ps_websocket_client = ps_websocket_client
        self.battle_manager = battle_manager
        self.challenge_dispatcher = challenge_dispatcher
        self.modes_by_prefix: Dict[str, BaseGameMode] = {}
        self.modes_by_id: Dict[str, BaseGameMode] = {}
        self._listener_tasks: List[asyncio.Task] = []

    def register_mode(self, mode: BaseGameMode):
        self.modes_by_id[mode.mode_id] = mode
        for prefix in mode.command_prefixes:
            self.modes_by_prefix[prefix.lower()] = mode
        logger.info(
            "Registered mode '{}' with prefixes: {}".format(
                mode.mode_id, mode.command_prefixes
            )
        )

    async def send_reply(self, room: str, player_display: str, msg_text: str):
        if not room:
            await self.ps_websocket_client.send_message(
                "", ["/pm {}, {}".format(player_display, msg_text)]
            )
        else:
            await self.ps_websocket_client.send_message(room, [msg_text])

    async def handle_incoming_text(
        self, username: str, text: str, room_context: str
    ):
        sender_userid = normalize_name(username)
        bot_userid = normalize_name(getattr(FoulPlayConfig, "username", ""))
        if sender_userid == bot_userid:
            return

        cleaned_text = text.strip()
        if not cleaned_text:
            return

        # 1. Showdown challenge rejection / cancellation PM check
        cleaned_lower = cleaned_text.lower()
        if (
            "rejected the challenge" in cleaned_lower
            or "cancelled the challenge" in cleaned_lower
        ):
            if self.battle_manager:
                pending = self.battle_manager.get_pending_challenge(
                    sender_userid
                )
                if pending:
                    await self.battle_manager.cancel_pending_challenge(
                        sender_userid
                    )
                    await self.send_reply(
                        pending["room_context"],
                        pending["player_display"],
                        "Your challenge was cancelled or rejected. You can retry whenever you're ready.",
                    )
            return

        # 2. Global command checks
        parts = cleaned_text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("@help", "!help"):
            await self._handle_help(username, room_context)
            return

        if cmd in ("@status", "!status"):
            await self._handle_global_status(sender_userid, username, room_context)
            return

        if cmd in ("@cancel", "@forfeit", "!cancel", "!forfeit"):
            await self._handle_global_cancel(sender_userid, username, room_context)
            return

        # 3. Match mode by command prefix
        # Check exact command or prefix match
        matched_mode = self.modes_by_prefix.get(cmd)
        if not matched_mode:
            for prefix, mode in self.modes_by_prefix.items():
                if cleaned_lower.startswith(prefix):
                    matched_mode = mode
                    break

        if matched_mode:
            await matched_mode.handle_command(
                sender_userid, username, cmd, args, room_context
            )

    async def _handle_help(self, username: str, room_context: str):
        lines = [
            "=== BattleBot Available Commands ===",
            "@battlehall [type] [level] - Challenge or resume Battle Hall run (e.g., @battlehall fire 50)",
            "@battlehall status - View your Battle Hall wins and active streak",
            "@battlehall cancel - Cancel your pending challenge or leave wait queue",
            "@battlehallreset - Reset all Battle Hall wins to 0",
            "@status - View career stats across all modes",
            "@cancel - Cancel any pending queue, challenge, or active match",
        ]
        for line in lines:
            await self.send_reply(room_context, username, line)

    async def _handle_global_status(
        self, player_userid: str, player_display: str, room_context: str
    ):
        status_lines = []
        for mode_id, mode in self.modes_by_id.items():
            st = await mode.get_player_status(player_userid)
            status_lines.append(st)

        if not status_lines:
            status_msg = "No active campaign game modes registered."
        else:
            status_msg = " | ".join(status_lines)

        await self.send_reply(room_context, player_display, status_msg)

    async def _handle_global_cancel(
        self, player_userid: str, player_display: str, room_context: str
    ):
        cancelled_anything = False

        # 1. Check wait queue
        if self.battle_manager and self.battle_manager.remove_from_wait_queue(
            player_userid
        ):
            await self.send_reply(
                room_context,
                player_display,
                "You have been removed from the battle wait queue.",
            )
            cancelled_anything = True

        # 2. Check pending challenges
        if (
            self.battle_manager
            and await self.battle_manager.cancel_pending_challenge(player_userid)
        ):
            await self.send_reply(
                room_context,
                player_display,
                "Your outgoing challenge was cancelled.",
            )
            cancelled_anything = True

        # 3. Check active battles
        if self.battle_manager and player_userid in self.battle_manager.battles_by_player:
            instance = self.battle_manager.battles_by_player[player_userid]
            await self.ps_websocket_client.send_message(
                instance.battle_tag, ["/forfeit"]
            )
            await self.send_reply(
                room_context,
                player_display,
                "Your active battle in {} was forfeited.".format(
                    instance.battle_tag
                ),
            )
            cancelled_anything = True

        # 4. Mode-specific session cleanup
        for mode in self.modes_by_id.values():
            await mode.cancel_session(player_userid, player_display, room_context)

        if not cancelled_anything:
            await self.send_reply(
                room_context,
                player_display,
                "You do not have any active queue, challenge, or battle to cancel.",
            )

    def start_listeners(self, lobby_room: str):
        t1 = asyncio.create_task(self._lobby_listener(lobby_room))
        t2 = asyncio.create_task(self._pm_listener())
        self._listener_tasks = [t1, t2]
        logger.info(
            "CommandDispatcher listeners started (lobby='{}').".format(
                lobby_room
            )
        )

    async def stop_listeners(self):
        for t in self._listener_tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._listener_tasks = []
        logger.info("CommandDispatcher listeners stopped.")

    async def _lobby_listener(self, lobby_room: str):
        while True:
            try:
                msg = await self.ps_websocket_client.receive_message(
                    room=lobby_room
                )
                parts = msg.split("|")
                if len(parts) >= 4 and parts[1] == "c":
                    username = parts[2]
                    text = "|".join(parts[3:])
                    await self.handle_incoming_text(
                        username, text, lobby_room
                    )
                elif len(parts) >= 5 and parts[1] == "c:":
                    username = parts[3]
                    text = "|".join(parts[4:])
                    await self.handle_incoming_text(
                        username, text, lobby_room
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in lobby listener: {}".format(e))

    async def _pm_listener(self):
        while True:
            try:
                msg = await self.ps_websocket_client.receive_message(room="")
                parts = msg.split("|")
                if len(parts) >= 5 and parts[1] == "pm":
                    sender = parts[2].strip()
                    text = "|".join(parts[4:])
                    await self.handle_incoming_text(sender, text, "")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in PM listener: {}".format(e))
