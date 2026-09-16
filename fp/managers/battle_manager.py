import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List
from config import FoulPlayConfig
from fp.modes.base import BaseGameMode, BattleConfiguration
from fp.websocket_client import PSWebsocketClient
import fp.run_battle
from fp.helpers import normalize_name

logger = logging.getLogger(__name__)


@dataclass
class BattleInstance:
    battle_tag: str
    player_userid: str
    player_display: str
    mode: BaseGameMode
    session_data: Dict[str, Any]
    battle_config: BattleConfiguration
    room_context: str
    task: Optional[asyncio.Task] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None


@dataclass
class QueuedPlayer:
    player_userid: str
    player_display: str
    mode: BaseGameMode
    battle_config: BattleConfiguration
    room_context: str
    session_data: Dict[str, Any]
    enqueued_at: float = field(default_factory=time.time)
    timeout_task: Optional[asyncio.Task] = None


class ActiveBattleManager:
    def __init__(
        self,
        ps_websocket_client: PSWebsocketClient,
        max_concurrent_battles: Optional[int] = None,
    ):
        self.ps_websocket_client = ps_websocket_client
        self.max_concurrent_battles = (
            max_concurrent_battles
            if max_concurrent_battles is not None
            else getattr(FoulPlayConfig, "max_concurrent_battles", 4)
        )
        self.battles_by_room: Dict[str, BattleInstance] = {}
        self.battles_by_player: Dict[str, BattleInstance] = {}
        self.pending_challenges: Dict[str, Dict[str, Any]] = {}
        self.wait_queue: List[QueuedPlayer] = []
        self.state_lock = asyncio.Lock()
        self.challenge_dispatcher: Optional[Any] = None
        self._orchestrator_task: Optional[asyncio.Task] = None

    def set_challenge_dispatcher(self, dispatcher: Any):
        self.challenge_dispatcher = dispatcher

    def active_battles_count(self) -> int:
        return len(self.battles_by_room)

    def is_at_capacity(self) -> bool:
        return self.active_battles_count() >= self.max_concurrent_battles

    def has_active_battle_or_challenge(self, player_userid: str) -> bool:
        norm_user = normalize_name(player_userid)
        if norm_user in self.battles_by_player:
            return True
        if norm_user in self.pending_challenges:
            return True
        for qp in self.wait_queue:
            if qp.player_userid == norm_user:
                return True
        return False

    def enqueue_wait(
        self,
        player_userid: str,
        player_display: str,
        mode: BaseGameMode,
        battle_config: BattleConfiguration,
        room_context: str,
        session_data: Dict[str, Any],
    ) -> int:
        norm_user = normalize_name(player_userid)
        qp = QueuedPlayer(
            player_userid=norm_user,
            player_display=player_display,
            mode=mode,
            battle_config=battle_config,
            room_context=room_context,
            session_data=session_data,
        )
        # 5 minute timeout watchdog (300 seconds)
        try:
            loop = asyncio.get_running_loop()
            qp.timeout_task = loop.create_task(
                self._wait_queue_timeout_watchdog(norm_user, qp.enqueued_at)
            )
        except RuntimeError:
            qp.timeout_task = None
        self.wait_queue.append(qp)
        return len(self.wait_queue)

    async def _wait_queue_timeout_watchdog(
        self, player_userid: str, enqueued_at: float
    ):
        await asyncio.sleep(300)
        async with self.state_lock:
            for i, qp in enumerate(self.wait_queue):
                if (
                    qp.player_userid == player_userid
                    and qp.enqueued_at == enqueued_at
                ):
                    self.wait_queue.pop(i)
                    logger.info(
                        "Removed {} from wait queue after 5-minute timeout.".format(
                            player_userid
                        )
                    )
                    if self.challenge_dispatcher:
                        await self.challenge_dispatcher.send_reply(
                            qp.room_context,
                            qp.player_display,
                            "Your position in the battle queue has expired after 5 minutes. You can request a challenge again when ready.",
                        )
                    break

    def remove_from_wait_queue(self, player_userid: str) -> bool:
        norm_user = normalize_name(player_userid)
        for i, qp in enumerate(self.wait_queue):
            if qp.player_userid == norm_user:
                if qp.timeout_task and not qp.timeout_task.done():
                    qp.timeout_task.cancel()
                self.wait_queue.pop(i)
                return True
        return False

    async def process_next_in_queue(self):
        async with self.state_lock:
            if not self.is_at_capacity() and self.wait_queue:
                next_player = self.wait_queue.pop(0)
                if next_player.timeout_task and not next_player.timeout_task.done():
                    next_player.timeout_task.cancel()

                logger.info(
                    "Popping {} from wait queue into active challenge.".format(
                        next_player.player_display
                    )
                )
                if self.challenge_dispatcher:
                    await self.challenge_dispatcher.send_reply(
                        next_player.room_context,
                        next_player.player_display,
                        "An arena slot is now available! Issuing your challenge now.",
                    )
                    await self.challenge_dispatcher._execute_challenge(
                        next_player.player_userid,
                        next_player.player_display,
                        next_player.mode,
                        next_player.battle_config,
                        next_player.room_context,
                        next_player.session_data,
                    )

    def register_pending_challenge(
        self,
        player_userid: str,
        player_display: str,
        mode: BaseGameMode,
        battle_config: BattleConfiguration,
        room_context: str,
        session_data: Dict[str, Any],
        challenge_sent_time: float,
    ):
        norm_user = normalize_name(player_userid)
        self.pending_challenges[norm_user] = {
            "player_userid": norm_user,
            "player_display": player_display,
            "mode": mode,
            "battle_config": battle_config,
            "room_context": room_context,
            "session_data": session_data,
            "challenge_sent_time": challenge_sent_time,
            "battle_started": False,
        }

    def get_pending_challenge(
        self, player_userid: str
    ) -> Optional[Dict[str, Any]]:
        return self.pending_challenges.get(normalize_name(player_userid))

    def remove_pending_challenge(self, player_userid: str):
        self.pending_challenges.pop(normalize_name(player_userid), None)

    async def cancel_pending_challenge(self, player_userid: str) -> bool:
        norm_user = normalize_name(player_userid)
        pending = self.pending_challenges.pop(norm_user, None)
        if pending:
            player_display = pending["player_display"]
            await self.ps_websocket_client.send_message(
                "", ["/cancelchallenge {}".format(player_display)]
            )
            await self.process_next_in_queue()
            return True
        return False

    def start_orchestrator(self):
        if self._orchestrator_task is None:
            self._orchestrator_task = asyncio.create_task(
                self._battle_orchestration_loop()
            )
            logger.info("ActiveBattleManager orchestrator loop started.")

    async def stop_orchestrator(self):
        if self._orchestrator_task is not None:
            self._orchestrator_task.cancel()
            try:
                await self._orchestrator_task
            except asyncio.CancelledError:
                pass
            self._orchestrator_task = None
            logger.info("ActiveBattleManager orchestrator loop stopped.")

    def _extract_opponent_userid(
        self, msg: str, bot_username: str
    ) -> Optional[str]:
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

    async def _battle_orchestration_loop(self):
        while True:
            try:
                battle_tag, msg = (
                    await self.ps_websocket_client.pending_battles_queue.get()
                )
                logger.info(
                    "Orchestrator received battle initialization for room: {}".format(
                        battle_tag
                    )
                )

                # Resolve opponent username (up to 10s wait)
                opponent_userid = self._extract_opponent_userid(
                    msg, FoulPlayConfig.username
                )
                messages_read = []
                while opponent_userid is None:
                    try:
                        m = await asyncio.wait_for(
                            self.ps_websocket_client.receive_message(
                                room=battle_tag
                            ),
                            timeout=10.0,
                        )
                        messages_read.append(m)
                        opponent_userid = self._extract_opponent_userid(
                            m, FoulPlayConfig.username
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Timeout waiting for opponent info in room {}".format(
                                battle_tag
                            )
                        )
                        break

                norm_opponent = (
                    normalize_name(opponent_userid) if opponent_userid else None
                )
                pending = None
                async with self.state_lock:
                    if (
                        norm_opponent
                        and norm_opponent in self.pending_challenges
                    ):
                        pending = self.pending_challenges.pop(norm_opponent)
                        pending["battle_started"] = True

                if pending is None:
                    logger.warning(
                        "Ignoring unexpected or unsolicited battle {} against {}".format(
                            battle_tag, norm_opponent
                        )
                    )
                    await self.ps_websocket_client.send_message(
                        battle_tag,
                        [
                            "Sorry, I am currently busy or this battle was not requested."
                        ],
                    )
                    await self.ps_websocket_client.leave_battle(battle_tag)
                    continue

                # Re-queue consumed messages so pokemon_battle receives them in order
                for m in messages_read:
                    self.ps_websocket_client.push_back_message(
                        m, room=battle_tag
                    )

                # Construct BattleInstance
                instance = BattleInstance(
                    battle_tag=battle_tag,
                    player_userid=pending["player_userid"],
                    player_display=pending["player_display"],
                    mode=pending["mode"],
                    session_data=pending["session_data"],
                    battle_config=pending["battle_config"],
                    room_context=pending["room_context"],
                    started_at=time.time(),
                )

                async with self.state_lock:
                    self.battles_by_room[battle_tag.lower()] = instance
                    self.battles_by_player[instance.player_userid] = instance

                logger.info(
                    "Spawning concurrent battle task for room {} vs {}".format(
                        battle_tag, instance.player_display
                    )
                )
                instance.task = asyncio.create_task(
                    self._run_battle_lifecycle(instance)
                )

                # IMMEDIATELY LOOPS BACK without blocking other rooms!
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(
                    "Error in battle orchestration loop: {}".format(e)
                )

    async def _run_battle_lifecycle(self, instance: BattleInstance):
        battle_tag = instance.battle_tag
        winner = None
        try:
            # Mode lifecycle start hook
            await instance.mode.on_battle_start(
                battle_tag, instance.session_data
            )

            # Run the battle asynchronously
            winner = await fp.run_battle.pokemon_battle(
                self.ps_websocket_client,
                instance.battle_config.pokemon_format,
                instance.battle_config.team_dict,
                battle_tag=battle_tag,
            )

            # Mode lifecycle end hook
            await instance.mode.on_battle_end(
                battle_tag, instance.session_data, winner
            )

        except asyncio.CancelledError:
            logger.info("Battle task in room {} was cancelled.".format(battle_tag))
            try:
                await self.ps_websocket_client.leave_battle(battle_tag)
            except Exception:
                pass
        except Exception as e:
            logger.exception(
                "Exception during battle in room {}: {}".format(battle_tag, e)
            )
            if self.challenge_dispatcher:
                await self.challenge_dispatcher.send_reply(
                    instance.room_context,
                    instance.player_display,
                    "An error occurred during your battle in {}. Your streak was preserved.".format(
                        battle_tag
                    ),
                )
        finally:
            async with self.state_lock:
                self.battles_by_room.pop(battle_tag.lower(), None)
                self.battles_by_player.pop(instance.player_userid, None)

            # Ensure websocket room queue is cleaned up
            self.ps_websocket_client.room_queues.pop(battle_tag.lower(), None)
            self.ps_websocket_client.room_buffers.pop(battle_tag.lower(), None)

            logger.info(
                "Battle room {} finished. Active battles remaining: {}".format(
                    battle_tag, self.active_battles_count()
                )
            )

            # Advance next waiting player in queue if available
            await self.process_next_in_queue()
