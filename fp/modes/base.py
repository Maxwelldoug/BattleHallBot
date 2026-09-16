from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class BattleConfiguration:
    pokemon_format: str
    team_dict: Optional[List[Dict[str, Any]]] = None
    team_packed: Optional[str] = None
    setup_commands: List[str] = field(default_factory=list)
    extra_info: Dict[str, Any] = field(default_factory=dict)


class BaseGameMode(ABC):
    @property
    @abstractmethod
    def mode_id(self) -> str:
        """Unique identifier (e.g. 'battlehall', 'battletower', 'gymleader')."""
        pass

    @property
    @abstractmethod
    def command_prefixes(self) -> List[str]:
        """Command prefixes that trigger this mode (e.g. ['@battlehall', '@bh'])."""
        pass

    @abstractmethod
    async def handle_command(
        self,
        player_userid: str,
        player_display: str,
        command: str,
        args: List[str],
        room_context: str,
    ):
        """Processes player input from lobby chat or PMs."""
        pass

    @abstractmethod
    async def prepare_battle(self, player_userid: str) -> Optional[BattleConfiguration]:
        """Returns format, team, and server pre-commands for the next battle."""
        pass

    @abstractmethod
    async def on_battle_start(self, battle_tag: str, session_data: Dict[str, Any]):
        """Hook called when the battle officially starts."""
        pass

    @abstractmethod
    async def on_battle_end(
        self, battle_tag: str, session_data: Dict[str, Any], winner: Optional[str]
    ):
        """Hook called when the battle finishes (handles wins, losses, rematches)."""
        pass

    @abstractmethod
    async def get_player_status(self, player_userid: str) -> str:
        """Returns player progress summary string."""
        pass

    @abstractmethod
    async def cancel_session(
        self, player_userid: str, player_display: str, room_context: str
    ) -> str:
        """Cancels any pending challenge or active session for the player."""
        pass
