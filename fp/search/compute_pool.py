import os
import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Optional
from config import FoulPlayConfig
from fp.battle import Battle

logger = logging.getLogger(__name__)


class MCTSComputePool:
    _instance: Optional["MCTSComputePool"] = None

    def __init__(
        self,
        max_workers: Optional[int] = None,
        max_parallel_searches: Optional[int] = None,
    ):
        if max_workers is None:
            max_workers = max(1, (os.cpu_count() or 4) - 1)
        if max_parallel_searches is None:
            max_parallel_searches = getattr(FoulPlayConfig, "max_parallel_searches", 2)

        self.max_workers = max_workers
        self.max_parallel_searches = max_parallel_searches
        self.executor = ProcessPoolExecutor(max_workers=self.max_workers)
        self.semaphore = asyncio.Semaphore(self.max_parallel_searches)
        self._is_shutdown = False
        logger.info(
            "Initialized MCTSComputePool (workers={}, max_parallel_searches={})".format(
                self.max_workers, self.max_parallel_searches
            )
        )

    @classmethod
    def get_instance(cls) -> "MCTSComputePool":
        if cls._instance is None or cls._instance._is_shutdown:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, pool: "MCTSComputePool"):
        cls._instance = pool

    def shutdown(self, wait: bool = True):
        if not self._is_shutdown:
            logger.info("Shutting down MCTSComputePool...")
            self.executor.shutdown(wait=wait)
            self._is_shutdown = True

    async def find_best_move_async(self, battle: Battle, timeout: float = 10.0) -> str:
        async with self.semaphore:
            loop = asyncio.get_running_loop()
            try:
                from fp.search.main import find_best_move

                best_move = await asyncio.wait_for(
                    loop.run_in_executor(None, find_best_move, battle, self.executor),
                    timeout=timeout,
                )
                return best_move
            except asyncio.TimeoutError:
                logger.warning(
                    "MCTS search timed out after {}s for battle {}. Falling back to default move.".format(
                        timeout, getattr(battle, "battle_tag", "unknown")
                    )
                )
                return self._fallback_move(battle)
            except Exception as e:
                logger.exception(
                    "Error in MCTS search for battle {}: {}. Falling back to default move.".format(
                        getattr(battle, "battle_tag", "unknown"), e
                    )
                )
                return self._fallback_move(battle)

    def _fallback_move(self, battle: Battle) -> str:
        if getattr(battle, "team_preview", False):
            return "teampreview 1"
        is_force_switch = getattr(battle, "force_switch", False)
        active = getattr(battle.user, "active", None) if getattr(battle, "user", None) else None
        active_fainted = (active is None) or (not active.is_alive()) or getattr(active, "fainted", False)

        if is_force_switch or active_fainted:
            if getattr(battle, "user", None) and getattr(battle.user, "reserve", None):
                for pkmn in battle.user.reserve:
                    if pkmn.is_alive() and not getattr(pkmn, "fainted", False):
                        return f"switch {pkmn.name}"

        if active and active.is_alive() and not getattr(active, "fainted", False):
            moves = getattr(active, "moves", [])
            for move in moves:
                if getattr(move, "current_pp", 1) > 0 and not getattr(move, "disabled", False):
                    return move.name
            if moves:
                return moves[0].name

        return "default"
