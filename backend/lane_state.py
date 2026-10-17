"""Lane state."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from backend.flags import ALL_COLLECTIONS_ON, CollectionPolicy
from backend.parse_collection import ParseCollection, parse_collection

if TYPE_CHECKING:
    from backend.lane_generation import LaneGeneration


@dataclass
class _ParseState:
    file_key: str
    collection: CollectionPolicy = field(default=ALL_COLLECTIONS_ON, kw_only=True)
    collect: ParseCollection = field(init=False)
    lane_generation: LaneGeneration | None = field(init=False, default=None)
    records: list[dict] = field(default_factory=list)
    tool_uses: list[dict] = field(default_factory=list)
    rate_limit_hits: list[dict] = field(default_factory=list)
    tool_result_is_error: dict[str, bool] = field(default_factory=dict)
    tool_result_error_info: dict[str, tuple[str, str | None]] = field(
        default_factory=dict)
    turns: list[dict] = field(default_factory=list)
    current_turn: dict | None = None
    current_turn_id: str | None = None
    pending_turn_begin_ts: datetime | None = None
    first_assistant_event_ts: datetime | None = None
    text_chars_since_turn: int = 0
    first_event_ts: datetime | None = None
    agent_role: str | None = None

    def __post_init__(self) -> None:
        self.collect = parse_collection(self.collection)
        if self.collection is ALL_COLLECTIONS_ON or self.collection.enabled("generation_speed"):
            from backend.lane_generation import LaneGeneration  # pylint: disable=import-outside-toplevel
            self.lane_generation = LaneGeneration()


@dataclass
class _CodexState(_ParseState):
    prev_usage: dict | None = None
    web_search_requests: int = 0
    model: str | None = None
    patch_target: tuple[int, str | None] | None = None
    last_rl_kind: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
    thread_seen: bool = False


@dataclass
class _KimiCodeState(_ParseState):
    """Steers awaiting context delivery."""

    pending_steers: list[list] = field(default_factory=list)
