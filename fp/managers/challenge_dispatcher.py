import asyncio
import time
import logging
from typing import Optional, Dict, Any
from fp.modes.base import BaseGameMode, BattleConfiguration
from fp.websocket_client import PSWebsocketClient

logger = logging.getLogger(__name__)


class ChallengeDispatcher:
    def __init__(self, ps_websocket_client: PSWebsocketClient):
        self.ps_websocket_client = ps_websocket_client
        self.challenge_lock = asyncio.Lock()
        self.battle_manager: Optional[Any] = None

    def set_battle_manager(self, battle_manager: Any):
        self.battle_manager = battle_manager

    async def send_reply(self, room: str, player_display: str, msg_text: str):
        if not room:
            await self.ps_websocket_client.send_message(
                "", ["/pm {}, {}".format(player_display, msg_text)]
            )
        else:
            await self.ps_websocket_client.send_message(room, [msg_text])

    async def dispatch_challenge(
        self,
        player_userid: str,
        player_display: str,
        mode: BaseGameMode,
        battle_config: BattleConfiguration,
        room_context: str,
        session_data: Dict[str, Any],
    ) -> bool:
        if self.battle_manager is None:
            raise RuntimeError("BattleManager not initialized on ChallengeDispatcher")

        # Check if player already has an ongoing battle or challenge
        if self.battle_manager.has_active_battle_or_challenge(player_userid):
            await self.send_reply(
                room_context,
                player_display,
                "You already have an active challenge or battle in progress. Use @cancel to cancel it.",
            )
            return False

        # Check if capacity is reached
        if self.battle_manager.is_at_capacity():
            pos = self.battle_manager.enqueue_wait(
                player_userid,
                player_display,
                mode,
                battle_config,
                room_context,
                session_data,
            )
            await self.send_reply(
                room_context,
                player_display,
                "The arena is currently at maximum capacity ({}/{}). You have been added to the queue (Position #{}).".format(
                    self.battle_manager.active_battles_count(),
                    self.battle_manager.max_concurrent_battles,
                    pos,
                ),
            )
            return True

        return await self._execute_challenge(
            player_userid,
            player_display,
            mode,
            battle_config,
            room_context,
            session_data,
        )

    async def _execute_challenge(
        self,
        player_userid: str,
        player_display: str,
        mode: BaseGameMode,
        battle_config: BattleConfiguration,
        room_context: str,
        session_data: Dict[str, Any],
    ) -> bool:
        async with self.challenge_lock:
            sent_time = time.time()
            logger.info(
                "Dispatching challenge for {} (mode={}, format={})".format(
                    player_display, mode.mode_id, battle_config.pokemon_format
                )
            )

            # 1. Send server setup commands (e.g. /battlehalllevel, /battlehalltype)
            for cmd in battle_config.setup_commands:
                await self.ps_websocket_client.send_message("", [cmd])

            # 2. Update active team for the bot
            if battle_config.team_packed:
                await self.ps_websocket_client.update_team(battle_config.team_packed)

            # 3. Issue the challenge command
            challenge_cmd = "/challenge {},{}".format(
                player_display, battle_config.pokemon_format
            )
            await self.ps_websocket_client.send_message("", [challenge_cmd])

            # 4. Register pending challenge in BattleManager
            self.battle_manager.register_pending_challenge(
                player_userid=player_userid,
                player_display=player_display,
                mode=mode,
                battle_config=battle_config,
                room_context=room_context,
                session_data=session_data,
                challenge_sent_time=sent_time,
            )

            # 5. Launch timeout watcher
            asyncio.create_task(
                self._challenge_timeout_monitor(player_userid, sent_time)
            )

            return True

    async def _challenge_timeout_monitor(
        self, player_userid: str, challenge_sent_time: float
    ):
        await asyncio.sleep(60)
        pending = self.battle_manager.get_pending_challenge(player_userid)
        if (
            pending
            and pending["challenge_sent_time"] == challenge_sent_time
            and not pending.get("battle_started", False)
        ):
            logger.info("Challenge to {} timed out after 60s.".format(player_userid))
            player_display = pending["player_display"]
            room_context = pending["room_context"]
            await self.ps_websocket_client.send_message(
                "", ["/cancelchallenge {}".format(player_display)]
            )
            self.battle_manager.remove_pending_challenge(player_userid)
            await self.send_reply(
                room_context,
                player_display,
                "Your challenge timed out. You can send @battlehall to retry whenever you're ready.",
            )
            # Slot may be available for queued players
            await self.battle_manager.process_next_in_queue()
